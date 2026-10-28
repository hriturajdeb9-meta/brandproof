"""Masks personal data and confidential terms before any text is sent to an AI provider.

    r = Redactor(text, confidential_terms)
    r.text          -> "Call [PHONE-1] about [CONFIDENTIAL-1]"   (what the AI sees)
    r.unmask(s)     -> puts the original values back into an AI answer
    r.counts        -> {"phone": 1, "confidential": 1}

Detection is done by code (regular expressions plus a Luhn check for card numbers), never by AI.
"""
from __future__ import annotations

import re

PATTERNS = [
    ("email", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}(?![\w-])")),
    ("card", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")),
    # the extra look-arounds stop a match inside a longer group of numbers, e.g. "1234 5678 9012 3456"
    ("aadhaar", re.compile(r"(?<!\d)(?<!\d[ -])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![ -]?\d)")),
    ("pan", re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])")),
    ("phone", re.compile(r"(?<![\w+])(?<!\d[ -])(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?![ -]?\d)|(?<![\w+])\+\d{1,3}[\s-]?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)")),
]
LABELS = {"email": "email address", "phone": "phone number", "card": "card number", "aadhaar": "Aadhaar number",
          "pan": "PAN", "confidential": "confidential term"}
SENSITIVE = {"card", "aadhaar", "pan"}  # never acceptable in published content


def luhn_ok(digits: str) -> bool:
    nums = [int(d) for d in digits if d.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def find_sensitive(text: str, confidential: list[str] | None = None) -> list[dict]:
    """All personal-data and confidential-term matches: [{kind, start, end, value}] without overlaps."""
    hits: list[dict] = []
    for term in sorted({t.strip() for t in confidential or [] if t and t.strip()}, key=len, reverse=True):
        for m in re.finditer(r"(?<![\w])" + re.escape(term) + r"(?![\w])", text, re.IGNORECASE):
            hits.append({"kind": "confidential", "start": m.start(), "end": m.end(), "value": m.group(0), "term": term})
    for kind, pat in PATTERNS:
        for m in pat.finditer(text):
            value = m.group(0)
            if kind == "card" and not luhn_ok(value):
                continue
            if kind == "aadhaar" and luhn_ok(value) and len(re.sub(r"\D", "", value)) > 12:
                continue
            hits.append({"kind": kind, "start": m.start(), "end": m.end(), "value": value})
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    out, last_end = [], -1
    for h in hits:
        if h["start"] >= last_end:
            out.append(h)
            last_end = h["end"]
    return out


class Redactor:
    def __init__(self, text: str, confidential: list[str] | None = None):
        self.original = text or ""
        self.hits = find_sensitive(self.original, confidential)
        self.mapping: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        by_value: dict[tuple[str, str], str] = {}
        parts, pos = [], 0
        for h in self.hits:
            key = (h["kind"], h["value"].lower())
            if key not in by_value:
                self.counts[h["kind"]] = self.counts.get(h["kind"], 0) + 1
                by_value[key] = f"[{h['kind'].upper()}-{self.counts[h['kind']]}]"
                self.mapping[by_value[key]] = h["value"]
            parts.append(self.original[pos:h["start"]])
            parts.append(by_value[key])
            pos = h["end"]
        parts.append(self.original[pos:])
        self.text = "".join(parts)

    @property
    def masked(self) -> int:
        return sum(self.counts.values())

    def unmask(self, s):
        if not isinstance(s, str) or not self.mapping:
            return s
        for token, value in self.mapping.items():
            s = s.replace(token, value)
        return s

    def summary(self) -> dict:
        return {"total": self.masked, "counts": dict(self.counts),
                "labels": [f"{n} {LABELS[k]}{'s' if n > 1 else ''}" for k, n in self.counts.items()]}
