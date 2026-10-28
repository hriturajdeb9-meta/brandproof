"""Loads the brand kit (rules, glossary, Claim Ledger, palette) into memory for a check run."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date

from .colour import hex_to_rgb
from .db import Database

DIMENSIONS = ["claims", "tone", "terminology", "messaging", "visual", "accessibility"]
DIMENSION_LABELS = {
    "claims": "Facts & claims",
    "tone": "Tone",
    "terminology": "Terminology",
    "messaging": "Messaging",
    "visual": "Visual",
    "accessibility": "Accessibility",
}
SEVERITIES = ["blocker", "major", "minor", "info", "ok"]


@dataclass
class Fact:
    id: str
    topic: str
    label: str
    statement: str
    value: float | None
    value_text: str
    approved_wording: str
    keywords: list[str]
    evidence: str
    owner: str
    valid_until: str

    def expired(self, today: date | None = None) -> bool:
        if not self.valid_until:
            return False
        try:
            return date.fromisoformat(self.valid_until) < (today or date.today())
        except ValueError:
            return False

    def as_evidence(self) -> dict:
        return {"id": self.id, "label": self.label, "statement": self.statement, "owner": self.owner,
                "valid_until": self.valid_until, "evidence": self.evidence, "expired": self.expired()}


@dataclass
class Kit:
    rules: dict[str, dict]
    glossary: list[dict]
    facts: dict[str, Fact]
    palette: list[dict]
    disclaimer: str
    hash: str
    private_terms: list[str] = field(default_factory=list)  # removed from drafts before any AI call
    compiled: dict[str, re.Pattern] = field(default_factory=dict)

    def rule(self, rule_id: str) -> dict | None:
        r = self.rules.get(rule_id)
        return r if r and r.get("active") else None

    def active_rules(self, dimension: str | None = None, methods: tuple[str, ...] | None = None) -> list[dict]:
        out = []
        for r in self.rules.values():
            if not r.get("active"):
                continue
            if dimension and r["dimension"] != dimension:
                continue
            if methods and r["method"] not in methods:
                continue
            out.append(r)
        return sorted(out, key=lambda r: r["id"])

    def pattern(self, rule_id: str) -> re.Pattern | None:
        r = self.rule(rule_id)
        if not r or not (r.get("pattern") or "").strip():
            return None
        if rule_id not in self.compiled:
            try:
                self.compiled[rule_id] = re.compile(r["pattern"], re.IGNORECASE)
            except re.error:
                self.compiled[rule_id] = re.compile(r"(?!x)x")  # invalid user regex never matches
        return self.compiled[rule_id]

    def palette_rgb(self) -> list[tuple[str, str, tuple[int, int, int]]]:
        out = []
        for p in self.palette:
            try:
                out.append((p["name"], p["hex"].upper(), hex_to_rgb(p["hex"])))
            except Exception:
                continue
        return out


def _num(v):
    try:
        return float(str(v).replace(",", "").strip()) if str(v).strip() else None
    except ValueError:
        return None


def load_kit(db: Database) -> Kit:
    rules = {r["id"]: r for r in db.all("SELECT * FROM rules")}
    glossary = db.all("SELECT * FROM glossary ORDER BY id")
    facts = {}
    for f in db.all("SELECT * FROM facts ORDER BY id"):
        facts[f["id"]] = Fact(
            id=f["id"], topic=f.get("topic") or f["id"], label=f["label"], statement=f["statement"],
            value=_num(f.get("value")), value_text=f.get("value") or "", approved_wording=f.get("approved_wording") or "",
            keywords=[k.strip().lower() for k in (f.get("keywords") or "").split(",") if k.strip()],
            evidence=f.get("evidence") or "", owner=f.get("owner") or "", valid_until=f.get("valid_until") or "",
        )
    palette = db.all("SELECT * FROM palette ORDER BY id")
    disclaimer = db.get_meta("disclaimer") or ""
    private = db.get_meta("private_terms")
    private_terms = parse_terms(DEFAULT_PRIVATE_TERMS if private is None else private)
    digest = hashlib.sha256(
        json.dumps([sorted(rules.items()), glossary, [f.__dict__ for f in facts.values()], palette, disclaimer],
                   sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    return Kit(rules=rules, glossary=glossary, facts=facts, palette=palette, disclaimer=disclaimer, hash=digest,
               private_terms=private_terms)


# Fictional internal names for the Paisaleaf demo; the brand manager edits the list on the Brand Kit page.
DEFAULT_PRIVATE_TERMS = "Project Banyan\nLeafLend\nPaisaleaf internal roadmap"


def parse_terms(raw: str) -> list[str]:
    seen, out = set(), []
    for line in (raw or "").replace(",", "\n").splitlines():
        t = " ".join(line.split())[:80]
        if len(t) >= 3 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:50]
