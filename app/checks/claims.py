"""Claim checking against the Claim Ledger.

AI (when available) finds the claims and says which ledger fact each one is about.
Code then decides the verdict: it compares numbers and expiry dates itself, so
the verdict never depends on the model's judgment.
"""
from __future__ import annotations

import re
from datetime import date

from ..kit import Fact, Kit
from ..textutil import find_span, match_case, segments
from .common import make_finding

SUPERLATIVE_RE = re.compile(
    r"(?<![\w])(best|#\s?1|no\.\s?1|number one|top[- ]rated|fastest|cheapest|highest|lowest|leading|largest|biggest|"
    r"unbeatable|better than|safest|smartest|most trusted|most popular|most loved|india'?s first|world'?s first)(?![\w])",
    re.IGNORECASE,
)
NUM_RE = re.compile(
    r"(?<![\w.#])(?<!no\. )(?<!No\. )(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(lakhs?|crores?|k\b|%|★|stars?|x\b)?",
    re.IGNORECASE,
)
FACTUAL_HINT_RE = re.compile(
    r"\b(insured|insurance|instant(ly)?|no fees?|zero fees?|free|withdraw|interest|returns?|rated|rating|savers|users)\b",
    re.IGNORECASE,
)
MULT = {"lakh": 1e5, "lakhs": 1e5, "crore": 1e7, "crores": 1e7, "k": 1e3}


def numbers(text: str) -> list[float]:
    out = []
    for m in NUM_RE.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if not unit and raw.isdigit() and 1900 <= val <= 2100:
            continue  # a year, not a claim number
        val *= MULT.get(unit.strip(), 1)
        out.append(val)
    return out


def num_match(a: float, b: float | None) -> bool:
    if b is None:
        return False
    return abs(a - b) <= max(0.005, abs(b) * 0.001)


def keyword_score(text: str, fact: Fact) -> float:
    low = text.lower()
    score = 0.0
    for kw in fact.keywords:
        if len(kw) < 2 and kw.isalnum():
            continue
        if re.search(r"(?<![\w])" + re.escape(kw) + r"(?![\w])", low):
            score += 1 + min(len(kw), 12) / 12
    if score > 0 and fact.value is not None and any(num_match(n, fact.value) for n in numbers(text)):
        score += 3
    return score


def best_fact(kit: Kit, text: str) -> Fact | None:
    scored = [(keyword_score(text, f), not f.expired(), f) for f in kit.facts.values()]
    scored = [s for s in scored if s[0] >= 1]
    if not scored:
        return None
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return scored[0][2]


def is_claim_like(text: str, kit: Kit) -> str | None:
    """Return the kind of claim a text looks like, or None."""
    prohibited = kit.pattern("R-CLM-01")
    if prohibited and prohibited.search(text):
        return "guarantee"
    if SUPERLATIVE_RE.search(text):
        return "superlative"
    if numbers(text) and re.search(r"[%₹★]|\b(p\.?a\.?|lakhs?|crores?|users|savers|days?|hours?|minutes?|seconds?|rated|stars?)\b", text, re.I):
        return "number"
    if FACTUAL_HINT_RE.search(text) and best_fact(kit, text):
        return "fact"
    return None


def code_claims(kit: Kit, text: str) -> list[dict]:
    claims = []
    for a, b in segments(text):
        seg = text[a:b]
        kind = is_claim_like(seg, kit)
        if kind:
            claims.append({"quote": seg, "start": a, "end": b, "kind": kind, "fact_id": None, "rewrite": "", "method": "code"})
    return claims


def judge(kit: Kit, quote: str, fact: Fact | None, kind: str, today: date | None = None,
          ai_inconsistent: bool = False) -> dict:
    """Decide a claim's verdict with code. Returns verdict, rule id, message, evidence, default fix."""
    today = today or date.today()
    nums = numbers(quote)
    sup = SUPERLATIVE_RE.search(quote)
    prohibited = kit.pattern("R-CLM-01")
    prohib = prohibited.search(quote) if prohibited else None
    evidence = []
    current = None
    verdict, rule_id, msg = None, None, ""
    if fact:
        topic = [f for f in kit.facts.values() if f.topic == fact.topic] or [fact]
        valid = sorted([f for f in topic if not f.expired(today)], key=lambda f: f.valid_until or "9999", reverse=True)
        expired = [f for f in topic if f.expired(today)]
        current = valid[0] if valid else None
        if sup and not any(sup.group(0).lower() in f.statement.lower() for f in topic):
            verdict, rule_id = "unsupported", "R-CLM-04"
            msg = (f"“{sup.group(0)}” needs evidence. No ledger fact says this. "
                   f"The closest fact is {fact.id}: “{fact.statement}”")
            evidence.append(fact.as_evidence())
        elif nums and any(f.value is not None for f in topic):
            hit = next((f for f in valid if any(num_match(n, f.value) for n in nums)), None)
            old = next((f for f in expired if any(num_match(n, f.value) for n in nums)), None)
            if hit:
                verdict, rule_id = "supported", "R-CLM-02"
                msg = f"Matches ledger fact {hit.id}: “{hit.statement}”"
                evidence.append(hit.as_evidence())
            elif old:
                verdict, rule_id = "outdated", "R-CLM-03"
                msg = f"This matches {old.id}, which expired on {old.valid_until}."
                if current:
                    msg += f" The current fact is {current.id}: “{current.statement}”"
                evidence += [old.as_evidence()] + ([current.as_evidence()] if current else [])
            else:
                verdict, rule_id = "contradicted", "R-CLM-02"
                ref = current or fact
                shown = ", ".join(m.group(0).strip() for m in NUM_RE.finditer(quote)) or ", ".join(_fmt(n) for n in nums)
                msg = f"{shown} does not match the Claim Ledger. {ref.id} says: “{ref.statement}”"
                evidence.append(ref.as_evidence())
        elif ai_inconsistent and current:
            verdict, rule_id = "contradicted", "R-CLM-02"
            msg = f"The AI judged this inconsistent with {current.id}: “{current.statement}”"
            evidence.append(current.as_evidence())
        elif not current:
            verdict, rule_id = "outdated", "R-CLM-03"
            msg = f"The ledger fact {fact.id} expired on {fact.valid_until}."
            evidence.append(fact.as_evidence())
        else:
            verdict, rule_id = "supported", "R-CLM-05"
            msg = f"Backed by ledger fact {current.id}: “{current.statement}”"
            evidence.append(current.as_evidence())
    else:
        if sup:
            verdict, rule_id = "unsupported", "R-CLM-04"
            msg = f"“{sup.group(0)}” is a superlative, and no ledger fact supports it."
        elif nums or kind in ("number", "comparison", "award", "fact"):
            verdict, rule_id = "unsupported", "R-CLM-05"
            msg = "This states a fact, but there is no matching fact in the Claim Ledger."
        elif kind == "guarantee":
            verdict, rule_id = "unsupported", "R-CLM-05"
            msg = "No ledger fact backs this claim."
    if prohib:
        extra = f" Also: {msg}" if verdict in ("contradicted", "outdated", "unsupported") else ""
        verdict, rule_id = "prohibited", "R-CLM-01"
        msg = f"“{prohib.group(0)}” is never allowed in savings messages.{extra}"
    if verdict is None:
        return {}
    # default fix from the ledger
    fix_fact = current or fact
    fix_text = ""
    if verdict in ("prohibited", "contradicted", "outdated") and fix_fact and fix_fact.approved_wording:
        fix_text = fix_fact.approved_wording
    elif verdict == "unsupported" and fact and not fact.expired(today) and fact.approved_wording:
        fix_text = fact.approved_wording
    return {"verdict": verdict, "rule_id": rule_id, "message": msg, "evidence": evidence, "fix_text": fix_text,
            "fact": fact}


def validate_rewrite(kit: Kit, quote: str, rewrite: str, locked: list[str]) -> tuple[bool, str]:
    """Check an AI-suggested rewrite with code before anyone sees it."""
    rewrite = (rewrite or "").strip()
    if not rewrite:
        return False, "empty"
    if len(rewrite) > max(240, len(quote) * 3):
        return False, "too long"
    prohibited = kit.pattern("R-CLM-01")
    if prohibited and prohibited.search(rewrite):
        return False, "still contains a prohibited promise"
    valid_values = [f.value for f in kit.facts.values() if f.value is not None and not f.expired()]
    for n in numbers(rewrite):
        if not any(num_match(n, v) for v in valid_values):
            return False, f"uses a number ({_fmt(n)}) that is not a current ledger fact"
    sup = SUPERLATIVE_RE.search(rewrite)
    if sup and not any(sup.group(0).lower() in f.statement.lower() for f in kit.facts.values()):
        return False, "adds an unsupported superlative"
    for phrase in locked:
        if phrase and phrase.lower() in quote.lower() and phrase.lower() not in rewrite.lower():
            return False, f"drops the locked phrase “{phrase}”"
    return True, ""


def claim_findings(kit: Kit, text: str, ai_claims: list[dict] | None, locked: list[str], target: str = "text",
                   line_boxes: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Build claim findings. `ai_claims` are already-parsed AI items (quote, fact_id, kind, rewrite)."""
    stats = {"ai_proposed": 0, "ai_discarded": 0, "code_added": 0}
    claims: list[dict] = []
    if ai_claims:
        for c in ai_claims:
            stats["ai_proposed"] += 1
            quote = (c.get("quote") or "").strip()
            span = find_span(text, quote)
            if not span:
                stats["ai_discarded"] += 1  # the model quoted words that are not in the draft
                continue
            fid = c.get("fact_id")
            if fid not in kit.facts:
                fid = None
            claims.append({"quote": text[span[0]:span[1]], "start": span[0], "end": span[1],
                           "kind": c.get("kind") or "other", "fact_id": fid,
                           "rewrite": c.get("rewrite") or "", "method": "ai+code",
                           "consistent": c.get("consistent")})
    for c in code_claims(kit, text):  # code safety net: add strong claims the AI missed
        if any(c["start"] < o["end"] and o["start"] < c["end"] for o in claims):
            continue
        if ai_claims is not None:
            stats["code_added"] += 1
        claims.append(c)

    findings = []
    for c in claims:
        fact = kit.facts.get(c["fact_id"]) if c.get("fact_id") else None
        if fact is None:
            fact = best_fact(kit, c["quote"])
        ai_says_inconsistent = c.get("consistent") is False and c.get("fact_id") == (fact.id if fact else None)
        res = judge(kit, c["quote"], fact, c["kind"], ai_inconsistent=ai_says_inconsistent)
        if not res:
            continue
        verdict = res["verdict"]
        if verdict == "supported" and c["method"] == "code" and c["kind"] == "fact":
            continue  # a sentence that merely mentions a topic is not a claim worth listing
        severity = {"prohibited": "blocker", "contradicted": "blocker", "outdated": "major",
                    "unsupported": "major", "supported": "ok"}[verdict]
        fix, note = None, ""
        if verdict != "supported":
            text_fix = ""
            if c.get("rewrite"):
                ok, why = validate_rewrite(kit, c["quote"], c["rewrite"], locked)
                if ok:
                    text_fix, note = c["rewrite"], "AI rewrite, checked by code against the ledger and locked phrases."
                else:
                    note = f"AI rewrite rejected by code ({why}); showing the ledger's approved wording."
            if not text_fix and res["fix_text"]:
                ok, why = validate_rewrite(kit, c["quote"], res["fix_text"], locked)
                text_fix = res["fix_text"] if ok else ""
                note = note or ("Approved wording from the Claim Ledger." if ok else f"Ledger wording would {why}.")
            if target == "text":
                if text_fix:
                    fix = {"type": "replace", "find": c["quote"], "text": match_case(c["quote"], text_fix)}
                else:
                    fix = {"type": "advice", "text": "Remove this claim, or add a fact with evidence to the Claim Ledger first."}
            else:
                fix = {"type": "advice", "text": f"Change the image text to: “{text_fix}”" if text_fix
                       else "Remove this claim from the image."}
        titles = {"prohibited": "Prohibited claim", "contradicted": "Claim contradicts the ledger",
                  "outdated": "Claim uses an expired fact", "unsupported": "Unsupported claim",
                  "supported": "Claim supported by the ledger"}
        box = None
        if target == "image" and line_boxes:
            box = _box_for(c["quote"], line_boxes)
        method = c["method"]
        if ai_says_inconsistent and verdict == "contradicted" and not numbers(c["quote"]):
            method = "ai"  # meaning judged by the model, not by number comparison
        findings.append(make_finding(
            kit, res["rule_id"], title=titles[verdict], message=res["message"], quote=c["quote"], target=target,
            span=(c["start"], c["end"]) if target == "text" else None, box=box, method=method,
            severity=severity, fix=fix, evidence=res["evidence"], verdict=verdict, fix_note=note,
        ))
    return findings, stats


def _box_for(quote: str, lines: list[dict]):
    q = quote.lower()
    hits = [l["box"] for l in lines if l.get("box") and (l["text"].lower() in q or q in l["text"].lower())]
    if not hits:
        return None
    return [min(b[0] for b in hits), min(b[1] for b in hits), max(b[2] for b in hits), max(b[3] for b in hits)]


def _fmt(n: float) -> str:
    if n >= 1e5 and n % 1e5 == 0:
        return f"{n / 1e5:g} lakh"
    return f"{n:g}"
