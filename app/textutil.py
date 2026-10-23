"""Text helpers: locating quotes, splitting sentences, readability and diffs."""
from __future__ import annotations

import difflib
import html
import re

_WS = re.compile(r"\s+")
_TRANS = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF\u2600-\u2604\u2607-\u27BF\u2B00-\u2B4F\uFE0F]"
)
ABBREVIATIONS = {"p.a.", "e.g.", "i.e.", "rs.", "approx.", "etc.", "vs.", "no.", "t&cs.", "dr.", "mr.", "ms.", "mrs."}


def norm(s: str) -> str:
    return _WS.sub(" ", (s or "").translate(_TRANS)).strip()


def find_span(text: str, quote: str, start: int = 0) -> tuple[int, int] | None:
    """Locate `quote` inside `text`. Tries exact, case-insensitive, then whitespace/punctuation tolerant."""
    if not text or not quote or not quote.strip():
        return None
    quote = quote.strip()
    i = text.find(quote, start)
    if i >= 0:
        return i, i + len(quote)
    i = text.lower().find(quote.lower(), start)
    if i >= 0:
        return i, i + len(quote)
    t2 = text.translate(_TRANS)  # 1:1 character mapping keeps offsets valid
    q2 = quote.translate(_TRANS)
    toks = [re.escape(t) for t in q2.split()]
    if not toks:
        return None
    m = re.compile(r"\s+".join(toks), re.IGNORECASE).search(t2, start)
    if m:
        return m.start(), m.end()
    return None


def segments(text: str) -> list[tuple[int, int]]:
    """Split text into sentence-like segments (start, end), trimmed of spaces and end punctuation.

    Boundaries: sentence punctuation followed by a capital/digit/emoji/end, line breaks and emoji.
    Abbreviations such as "p.a." do not end a sentence.
    """
    spans = []
    n = len(text)
    seg_start = 0
    i = 0
    while i < n:
        ch = text[i]
        cut_at = None
        skip_to = None
        if ch == "\n":
            cut_at, skip_to = i, i + 1
        elif EMOJI_RE.match(ch):
            cut_at, skip_to = i, i + 1
        elif ch in ".!?":
            j = i
            while j + 1 < n and text[j + 1] in ".!?":
                j += 1
            k = j + 1
            while k < n and text[k] in " \t":
                k += 1
            word_start = max(text.rfind(" ", 0, i), text.rfind("\n", 0, i)) + 1
            word = text[word_start : j + 1].lower()
            nxt = text[k] if k < n else ""
            is_decimal = ch == "." and i + 1 < n and text[i + 1].isdigit()
            if not is_decimal and word not in ABBREVIATIONS and (
                k >= n or nxt.isupper() or nxt.isdigit() or nxt in "\n\"'(*" or EMOJI_RE.match(nxt or " ")
            ):
                cut_at, skip_to = j + 1, j + 1
            elif not is_decimal and word not in ABBREVIATIONS and k < n and nxt == "₹":
                cut_at, skip_to = j + 1, j + 1
        if cut_at is not None:
            _add_segment(text, seg_start, cut_at, spans)
            seg_start = skip_to
            i = skip_to
            continue
        i += 1
    _add_segment(text, seg_start, n, spans)
    return spans


def _add_segment(text, a, b, out):
    while a < b and (text[a].isspace() or EMOJI_RE.match(text[a])):
        a += 1
    while b > a and (text[b - 1].isspace() or text[b - 1] in ".!?" or EMOJI_RE.match(text[b - 1])):
        b -= 1
    if b - a >= 2:
        out.append((a, b))


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ɏ']+|\d+(?:[.,]\d+)*", text)


def _syllables(word: str) -> int:
    w = word.lower().strip("'")
    if not w or w.isdigit():
        return 1
    groups = re.findall(r"[aeiouy]+", w)
    count = len(groups)
    if w.endswith("e") and not w.endswith(("le", "ee", "ye")) and count > 1:
        count -= 1
    return max(1, count)


def reading_ease(text: str) -> tuple[float | None, int, int]:
    """Flesch reading ease. Returns (score or None if too short, word count, sentence count)."""
    segs = [text[a:b] for a, b in segments(text)]
    ws = [w for w in words(text) if not w[0].isdigit()]
    if len(ws) < 30 or not segs:
        return None, len(ws), len(segs)
    syll = sum(_syllables(w) for w in ws)
    score = 206.835 - 1.015 * (len(ws) / len(segs)) - 84.6 * (syll / len(ws))
    return round(score, 1), len(ws), len(segs)


def word_diff_html(old: str, new: str) -> str:
    """HTML word diff (removed text in <del>, added text in <ins>).

    Short unchanged islands between two changes are folded into the change, so a rewritten phrase
    reads as one replacement instead of a word-by-word mix.
    """
    a = re.findall(r"\s+|[^\s]+", old or "")
    b = re.findall(r"\s+|[^\s]+", new or "")
    ops = [list(op) for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]
    merged: list[list] = []
    for i, op in enumerate(ops):
        tag, i1, i2, j1, j2 = op
        island = tag == "equal" and 0 < i < len(ops) - 1 and len([t for t in a[i1:i2] if t.strip()]) <= 2
        if (tag != "equal" or island) and merged and merged[-1][0] == "change":
            merged[-1][2], merged[-1][4] = i2, j2
        elif tag != "equal":
            merged.append(["change", i1, i2, j1, j2])
        else:
            merged.append(op)
    out = []
    for tag, i1, i2, j1, j2 in merged:
        if tag == "equal":
            out.append(html.escape("".join(a[i1:i2])))
            continue
        removed, added = "".join(a[i1:i2]), "".join(b[j1:j2])
        if removed.strip():
            out.append("<del>" + html.escape(removed) + "</del>")
        if added.strip():
            if removed.strip():
                out.append(" ")
            out.append("<ins>" + html.escape(added) + "</ins>")
        elif added:
            out.append(html.escape(added))
    return "".join(out)


def match_case(template: str, text: str) -> str:
    """Give `text` the same first-letter case as `template`."""
    if not text or not template:
        return text
    first = template.lstrip()[:1]
    if first.isupper() and text[0].islower():
        return text[0].upper() + text[1:]
    if first.islower() and text[0].isupper() and not text.split()[0].isupper():
        # keep proper nouns such as Paisaleaf capitalised
        if not text.startswith(("Paisaleaf", "DICGC", "Google")):
            return text[0].lower() + text[1:]
    return text


def tidy_spaces(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.!?;:])", r"\1", text)
    # a removal can leave a sentence starting with a comma: "Hurry, this is" -> ", this is" -> "This is"
    text = re.sub(r"(^|[.!?]\s+|\n\s*)[,;:]\s*(\w)", lambda m: m.group(1) + m.group(2).upper(), text)
    text = re.sub(r"([!?])\1+", r"\1", text)
    return text.strip()
