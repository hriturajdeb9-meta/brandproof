"""Keyword retrieval (BM25) used when a Brand Kit is large.

Small kits are sent to the AI whole. When a workspace has many AI-judged rules (for example after importing a
long guideline), only the rules most relevant to the draft are sent, so prompts stay short, fast and focused.
The same scoring finds the guideline passages behind a rule. Pure Python; no vector database needed.
"""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = set("""a an and are as at be but by for from has have i if in into is it its of on or our so that the their
them then there these they this to us was we were what when which who will with you your not no do does don't
must should can may use using used than more most any all each""".split())


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9₹%★]+(?:'[a-z]+)?", (text or "").lower())
    out = []
    for w in words:
        if w in STOP or len(w) < 2 and not w.isdigit():
            continue
        for suf in ("ing", "ed", "es", "s"):  # tiny stemmer: savings ~ saving ~ save
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w)
    return out


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.4, b: float = 0.75):
        self.docs = [tokens(d) for d in docs]
        self.k1, self.b = k1, b
        self.avg = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    def scores(self, query: str) -> list[float]:
        q = set(tokens(query))
        out = []
        for d, tf in zip(self.docs, self.tf):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / (self.avg or 1)))
            out.append(s)
        return out


def rule_doc(r: dict) -> str:
    return " ".join(str(r.get(k) or "") for k in ("title", "description", "do_example", "dont_example"))


def select_rules(rules: list[dict], draft: str, limit: int = 20) -> tuple[list[dict], bool]:
    """Return (rules to send, whether a selection was made)."""
    if len(rules) <= limit:
        return rules, False
    scores = BM25([rule_doc(r) for r in rules]).scores(draft)
    sev = {"blocker": 0.6, "major": 0.3}
    ranked = sorted(zip(rules, scores), key=lambda x: x[1] + sev.get(x[0].get("severity"), 0), reverse=True)
    return [r for r, _ in ranked[:limit]], True


def best_passages(pages: list[str], query: str, top: int = 3) -> list[tuple[int, float]]:
    """(page index, score) of the pages most relevant to the query."""
    if not pages:
        return []
    scores = BM25(pages).scores(query)
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    return [(i, s) for i, s in ranked[:top] if s > 0]
