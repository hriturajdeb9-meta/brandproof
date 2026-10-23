"""Checks that run as plain code: fast, free and repeatable."""
from __future__ import annotations

import re

from ..kit import Kit
from ..textutil import EMOJI_RE, match_case, reading_ease, segments
from .common import bounded, make_finding, parse_fix_hint

HYPE_EMOJI = set("🚀🔥💰🤑💸💯📈🤩⚡💥🎯🏆")
ACRONYMS = {"EMI", "UPI", "KYC", "RBI", "DICGC", "SEBI", "IRDAI", "ATM", "PAN", "NRI", "GST", "TDS", "FAQ", "APP",
            "OTP", "NEFT", "IMPS", "RTGS", "INR", "USD", "CEO", "T&C", "T&CS", "PDF", "IPO", "AI"}
MONEY_RE = re.compile(r"[\d₹%]|\b(returns?|interest|earn|sav(e|ings?)|rates?|money|profit|wealth|invest)\b", re.I)


def check_terminology(kit: Kit, text: str, target: str = "text") -> list[dict]:
    out = []
    taken: list[tuple[int, int]] = []
    for g in kit.glossary:
        preferred = (g.get("preferred") or "").strip()
        rule_id = g.get("rule_id") or "R-TRM-01"
        if not preferred or not kit.rule(rule_id):
            continue
        variants = sorted({v.strip() for v in (g.get("avoid") or "").split(",") if v.strip()}, key=len, reverse=True)
        for v in variants:
            for m in re.finditer(bounded(v), text, re.IGNORECASE):
                found = m.group(0)
                if found == preferred:
                    continue  # already correct (case-sensitive match on the preferred form)
                if any(m.start() < b and a < m.end() for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                replacement = match_case(found, preferred)
                fix = {"type": "replace", "find": found, "text": replacement} if target == "text" else {
                    "type": "advice", "text": f"Change “{found}” to “{replacement}” in the design."}
                out.append(make_finding(
                    kit, rule_id, title=f"Use “{replacement}”, not “{found}”",
                    message=g.get("note") or kit.rules[rule_id]["title"], quote=found, target=target,
                    span=(m.start(), m.end()) if target == "text" else None, fix=fix,
                ))
    return out


def check_pattern_rules(kit: Kit, text: str, target: str = "text") -> list[dict]:
    """Generic engine for rules that carry a regex pattern (tone, messaging, accessibility, custom rules)."""
    out = []
    for r in kit.active_rules():
        if r["dimension"] == "claims" or r["method"] not in ("code", "code+ai"):
            continue
        pat = kit.pattern(r["id"])
        if not pat:
            continue
        kind, payload = parse_fix_hint(r.get("fix_hint"))
        if kind == "append":
            continue  # required-text rules are handled by check_required_text
        for m in pat.finditer(text):
            found = m.group(0)
            fix = None
            if target == "text":
                if kind == "remove":
                    fix = {"type": "remove", "find": found, "text": ""}
                elif kind == "replace":
                    fix = {"type": "replace", "find": found, "text": match_case(found, payload)}
                elif kind == "map":
                    rep = payload.get(found.lower())
                    if rep:
                        fix = {"type": "replace", "find": found, "text": match_case(found, rep)}
                elif kind == "advice":
                    fix = {"type": "advice", "text": payload}
            else:
                fix = {"type": "advice", "text": "Change this wording in the design."}
            out.append(make_finding(
                kit, r["id"], title=f"{r['title']}: “{found}”", message=r.get("description") or r["title"],
                quote=found, target=target, span=(m.start(), m.end()) if target == "text" else None, fix=fix,
            ))
    return out


def check_required_text(kit: Kit, text: str) -> list[dict]:
    """Rules like R-MSG-03: if the trigger pattern appears, the required text must be present."""
    out = []
    low = text.lower()
    for r in kit.active_rules():
        kind, payload = parse_fix_hint(r.get("fix_hint"))
        if kind != "append" or not payload:
            continue
        pat = kit.pattern(r["id"])
        m = pat.search(text) if pat else None
        if not m:
            continue
        required = payload.strip()
        core = required.lstrip("*").strip().rstrip(".").lower()
        if core and core in low:
            continue
        out.append(make_finding(
            kit, r["id"], title="Required disclaimer is missing",
            message=f"This message mentions “{m.group(0)}”, so it must include: {required}",
            quote=m.group(0), span=(m.start(), m.end()), fix={"type": "append", "text": required},
        ))
    return out


def check_caps_and_emoji(kit: Kit, text: str) -> list[dict]:
    if not kit.rule("R-TON-02"):
        return []
    out = []
    allow = ACRONYMS | {g["preferred"].upper() for g in kit.glossary if g["preferred"].isupper()}
    for a, b in segments(text):
        seg = text[a:b]
        if not MONEY_RE.search(seg):
            continue
        caps = [m for m in re.finditer(r"(?<![\w&])[A-Z]{4,}(?![\w&])", seg) if m.group(0) not in allow]
        if not caps:
            continue
        s0, s1 = a + caps[0].start(), a + caps[-1].end()
        quote = text[s0:s1]
        fixed = quote.lower()
        if s0 == a:
            fixed = fixed[:1].upper() + fixed[1:]
        fixed = re.sub(r"\bpaisaleaf\b", "Paisaleaf", fixed)
        words = ", ".join(f"“{m.group(0)}”" for m in caps[:4]) + ("…" if len(caps) > 4 else "")
        out.append(make_finding(
            kit, "R-TON-02", title=f"All-caps in a money message: {words}",
            message="Money messages use sentence case so they read calm, not shouty.", quote=quote,
            span=(s0, s1), fix={"type": "replace", "find": quote, "text": fixed},
        ))
    # hype emoji anywhere in a draft that talks about money
    if MONEY_RE.search(text):
        for i, ch in enumerate(text):
            if ch in HYPE_EMOJI:
                out.append(make_finding(
                    kit, "R-TON-02", title=f"Hype emoji {ch} in a money message",
                    message="Emoji such as 🚀 🔥 💰 make returns sound like hype.", quote=ch, span=(i, i + 1),
                    fix={"type": "remove", "find": ch, "text": ""},
                ))
    return out


def check_readability(kit: Kit, text: str) -> list[dict]:
    if not kit.rule("R-ACC-04"):
        return []
    out = []
    score, n_words, _ = reading_ease(text)
    if score is not None and score < 50:
        out.append(make_finding(
            kit, "R-ACC-04", title=f"Hard to read (reading ease {score:.0f}, aim for 60+)",
            message=f"Flesch reading ease is {score:.0f} across {n_words} words. Shorter sentences and everyday words help first-time savers.",
            target="draft", fix={"type": "advice", "text": "Split long sentences and swap technical words for everyday ones."},
            evidence=[{"label": "Reading ease", "value": score}],
        ))
    for a, b in segments(text):
        n = len(re.findall(r"\S+", text[a:b]))
        if n > 30:
            out.append(make_finding(
                kit, "R-ACC-04", title=f"Long sentence ({n} words)",
                message="Sentences over 30 words are hard to follow. Try splitting this one.", quote=text[a:b],
                span=(a, b), fix={"type": "advice", "text": "Split this sentence into two or three shorter ones."},
            ))
    return out


def check_alt_text(kit: Kit, has_image: bool, alt: str, suggested_alt: str | None) -> list[dict]:
    if not has_image or not kit.rule("R-ACC-02"):
        return []
    alt = (alt or "").strip()
    if not alt:
        if suggested_alt:
            fix = {"type": "alt", "text": suggested_alt}
            note = "Alt text suggested by AI from the image. Check it before accepting."
        else:
            fix = {"type": "advice", "text": "Describe what the image shows and any text in it, in one or two sentences."}
            note = ""
        return [make_finding(
            kit, "R-ACC-02", title="Image has no alt text",
            message="Screen-reader users can't see the image. Alt text describes it and any words in it.",
            target="alt", fix=fix, fix_note=note, method="code",
        )]
    if len(alt) > 250:
        return [make_finding(
            kit, "R-ACC-02", title="Alt text is very long", severity="minor",
            message=f"Alt text is {len(alt)} characters. Keep it under about 250 so it is quick to listen to.",
            target="alt", fix={"type": "advice", "text": "Shorten the alt text to the key point and any text in the image."},
        )]
    return []


def strip_emoji(text: str) -> str:
    return EMOJI_RE.sub("", text)
