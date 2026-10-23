"""Shared helpers for building findings."""
from __future__ import annotations

import hashlib
import re

from ..kit import Kit

METHOD_LABELS = {
    "code": "Code check",
    "ai+code": "AI, verified by code",
    "ai": "AI-assessed",
}


def make_finding(kit: Kit, rule_id: str, *, title: str, message: str, quote: str | None = None,
                 target: str = "text", span: tuple[int, int] | None = None, box=None, method: str = "code",
                 confidence: str = "high", severity: str | None = None, fix: dict | None = None,
                 evidence: list | None = None, verdict: str | None = None, needs_human: bool = False,
                 fix_note: str = "") -> dict:
    rule = kit.rules.get(rule_id) or {}
    sev = severity or rule.get("severity", "minor")
    key_src = "|".join([rule_id, target, (quote or "").lower(), str(span[0] if span else ""), title])
    return {
        "key": hashlib.sha1(key_src.encode()).hexdigest()[:10],
        "dimension": rule.get("dimension", "messaging"),
        "severity": sev,
        "rule_id": rule_id,
        "rule_title": rule.get("title", rule_id),
        "rule_text": rule.get("description", ""),
        "source": rule.get("source", ""),
        "title": title,
        "message": message,
        "quote": quote,
        "target": target,
        "start": span[0] if span else None,
        "end": span[1] if span else None,
        "box": box,
        "method": method,
        "method_label": METHOD_LABELS.get(method, method),
        "confidence": confidence,
        "needs_human": needs_human,
        "verdict": verdict,
        "evidence": evidence or [],
        "fix": fix,
        "fix_note": fix_note,
    }


def bounded(term: str) -> str:
    """Regex for a term that must not touch other word characters (works with punctuation such as F.D.)."""
    return r"(?<![\w])" + re.escape(term.strip()) + r"(?![\w])"


def overlaps(a: dict, b: dict) -> bool:
    if a.get("target") != b.get("target") or a.get("start") is None or b.get("start") is None:
        return False
    return a["start"] < b["end"] and b["start"] < a["end"]


def parse_fix_hint(hint: str | None) -> tuple[str, object]:
    """'remove' | 'replace: text' | 'append: text' | 'map: a=>b; c=>d' | free advice text."""
    hint = (hint or "").strip()
    if not hint:
        return "none", None
    low = hint.lower()
    if low == "remove":
        return "remove", None
    for kind in ("replace", "append"):
        if low.startswith(kind + ":"):
            return kind, hint.split(":", 1)[1].strip()
    if low.startswith("map:"):
        pairs = {}
        for part in hint.split(":", 1)[1].split(";"):
            if "=>" in part:
                a, b = part.split("=>", 1)
                pairs[a.strip().lower()] = b.strip()
        return "map", pairs
    return "advice", hint
