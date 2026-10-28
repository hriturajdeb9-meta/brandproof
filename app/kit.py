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
    confidential: list[str] = field(default_factory=list)
    workspace_id: int | None = None
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


def load_kit(db: Database, workspace_id: int) -> Kit:
    ws = (workspace_id,)
    rules = {r["id"]: r for r in db.all("SELECT * FROM rules WHERE workspace_id = ?", ws)}
    glossary = db.all("SELECT * FROM glossary WHERE workspace_id = ? ORDER BY id", ws)
    facts = {}
    for f in db.all("SELECT * FROM facts WHERE workspace_id = ? ORDER BY id", ws):
        facts[f["id"]] = Fact(
            id=f["id"], topic=f.get("topic") or f["id"], label=f["label"], statement=f["statement"],
            value=_num(f.get("value")), value_text=f.get("value") or "", approved_wording=f.get("approved_wording") or "",
            keywords=[k.strip().lower() for k in (f.get("keywords") or "").split(",") if k.strip()],
            evidence=f.get("evidence") or "", owner=f.get("owner") or "", valid_until=f.get("valid_until") or "",
        )
    palette = db.all("SELECT * FROM palette WHERE workspace_id = ? ORDER BY id", ws)
    confidential = [c["term"] for c in db.all("SELECT term FROM confidential WHERE workspace_id = ? ORDER BY id", ws)
                    if (c.get("term") or "").strip()]
    disclaimer = ""
    for r in sorted(rules.values(), key=lambda r: r["id"]):
        hint = (r.get("fix_hint") or "").strip()
        if r.get("active") and hint.lower().startswith("append:"):
            disclaimer = hint.split(":", 1)[1].strip()
            break
    for r in rules.values():
        r.pop("updated_at", None)
    digest = hashlib.sha256(
        json.dumps([sorted(rules.items()), [{k: v for k, v in g.items() if k != "updated_at"} for g in glossary],
                    [f.__dict__ for f in facts.values()], [(p["name"], p["hex"]) for p in palette], confidential],
                   sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    return Kit(rules=rules, glossary=glossary, facts=facts, palette=palette, disclaimer=disclaimer, hash=digest,
               confidential=confidential, workspace_id=workspace_id)
