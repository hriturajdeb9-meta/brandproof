"""Removes personal data and confidential terms from text before it is sent to an AI provider.

Each match is replaced by a numbered placeholder such as [EMAIL-1]. The same placeholders in the AI's answer
are put back afterwards, so quotes still match the draft and code verification works as before.
"""
from __future__ import annotations

import re

LABELS = {"email": "email address", "phone": "phone number", "pan": "PAN", "aadhaar": "Aadhaar number",
          "card": "card number", "upi": "UPI ID", "confidential": "confidential term"}

# Order matters: longer, more specific patterns first so a card number is not taken as a phone number.
PATTERNS = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")),
    ("upi", re.compile(r"\b[\w.-]{2,}@(?:ok)?(?:axis|hdfc|icici|sbi|ybl|paytm|upi|ibl|apl|kotak)\b", re.I)),
    ("card", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")),
    ("aadhaar", re.compile(r"(?<!\d)(?<!\d[ -])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![ -]?\d)")),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("phone", re.compile(r"(?<![\w.])(?<!\d[ -])(?:\+?91[ -]?)?[6-9]\d{4}[ -]?\d{5}(?![ -]?\d|\.\d|%)")),
]

DRAFT_BLOCK = re.compile(r"(<<<DRAFT\n)(.*?)(\nDRAFT>>>)", re.S)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_text(text: str, terms: list[str], mapping: dict[str, str], counts: dict[str, int]) -> str:
    def sub(kind: str, value: str) -> str:
        for ph, v in mapping.items():
            if v == value:
                return ph
        counts[kind] = counts.get(kind, 0) + 1
        ph = f"[{kind.upper()}-{counts[kind]}]"
        mapping[ph] = value
        return ph

    for kind, pattern in PATTERNS:
        def repl(m: re.Match, kind=kind) -> str:
            if kind == "card" and not _luhn(re.sub(r"\D", "", m.group())):
                return m.group()
            return sub(kind, m.group())
        text = pattern.sub(repl, text)
    for term in sorted({t.strip() for t in terms if t and t.strip()}, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", lambda m: sub("confidential", m.group()), text, flags=re.I)
    return text


def redact(prompt: str, terms: list[str] | None = None) -> tuple[str, dict[str, str], dict[str, int]]:
    """Redact the draft part of a prompt (the whole prompt if it has no DRAFT block).

    Returns (redacted prompt, placeholder -> original, counts by kind)."""
    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}
    terms = terms or []
    if DRAFT_BLOCK.search(prompt):
        out = DRAFT_BLOCK.sub(lambda m: m.group(1) + _redact_text(m.group(2), terms, mapping, counts) + m.group(3),
                              prompt)
    else:
        out = _redact_text(prompt, terms, mapping, counts)
    return out, mapping, counts


def restore(obj, mapping: dict[str, str]):
    """Put the original values back into every string of an AI answer."""
    if not mapping:
        return obj
    if isinstance(obj, str):
        for ph, value in mapping.items():
            obj = obj.replace(ph, value)
        return obj
    if isinstance(obj, list):
        return [restore(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: restore(v, mapping) for k, v in obj.items()}
    return obj


def describe(counts: dict[str, int]) -> str:
    """'2 email addresses, 1 phone number'."""
    parts = []
    for kind, n in counts.items():
        label = LABELS.get(kind, kind)
        parts.append(f"{n} {label}{'' if n == 1 else 'es' if label.endswith('s') else 's'}")
    return ", ".join(parts)
