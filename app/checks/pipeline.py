"""Runs every check on one draft version and returns findings plus a summary of how the run went."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from ..ai import AIGateway
from ..kit import DIMENSIONS, SEVERITIES, Kit
from .ai_checks import ai_claims, ai_read_image, ai_second_opinion, ai_tone
from .claims import claim_findings
from .code_checks import (check_alt_text, check_caps_and_emoji, check_pattern_rules, check_readability,
                          check_required_text, check_terminology)
from .image import check_palette, check_text_contrast, looks_like_photo, open_rgb

PENALTY = {"blocker": 40, "major": 20, "minor": 5, "info": 0, "ok": 0}


def run_checks(ai: AIGateway, kit: Kit, meta: dict, body: str, image: bytes | None, image_mime: str | None,
               alt: str) -> dict:
    t0 = time.time()
    body = body or ""
    has_text = bool(body.strip())
    findings: list[dict] = []
    ai_calls: list[dict] = []
    notes: list[str] = []
    stats = {"ai_proposed": 0, "ai_discarded": 0, "code_added": 0, "needs_human": 0, "second_checked": 0,
             "second_disagreed": 0}
    use_ai = ai.enabled

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_claims = pool.submit(ai_claims, ai, kit, body, meta) if (has_text and use_ai) else None
        fut_image = pool.submit(ai_read_image, ai, image, image_mime or "image/png") if (image and use_ai) else None

        # ---- code checks on the text
        code: list[dict] = []
        if has_text:
            code += check_terminology(kit, body)
            code += check_pattern_rules(kit, body)
            code += check_required_text(kit, body)
            code += check_caps_and_emoji(kit, body)
            code += check_readability(kit, body)
        findings += code
        fut_tone = pool.submit(ai_tone, ai, kit, body, meta, code) if (has_text and use_ai) else None

        # ---- claims: AI finds them (if available), code decides the verdict
        if has_text:
            ai_list = None
            if fut_claims:
                ai_list, info = fut_claims.result()
                ai_calls.append(info)
                if ai_list is None:
                    notes.append("AI claim finder unavailable, so claims were found with code patterns only.")
            cf, cstats = claim_findings(kit, body, ai_list, meta.get("locked") or [])
            findings += cf
            for k in ("ai_proposed", "ai_discarded", "code_added"):
                stats[k] += cstats.get(k, 0)

        # ---- tone (AI)
        if fut_tone:
            tone, tinfo = fut_tone.result()
            ai_calls.append({k: tinfo.get(k) for k in ("purpose", "provider", "model", "ok", "cached", "error", "ms",
                                                         "redacted")})
            findings += tone
            stats["ai_proposed"] += tinfo.get("ai_proposed", 0)
            stats["ai_discarded"] += tinfo.get("ai_discarded", 0)
            stats["needs_human"] += tinfo.get("needs_human", 0)
        elif has_text and not use_ai:
            notes.append("AI tone check skipped (AI is off). Code tone rules still ran.")

        # ---- second opinion on every AI-assessed finding (code applies the answer)
        if has_text and use_ai:
            sinfo = ai_second_opinion(ai, kit, body, findings)
            if sinfo:
                ai_calls.append({k: sinfo.get(k) for k in ("purpose", "provider", "model", "ok", "cached", "error",
                                                           "ms", "redacted")})
                stats["second_checked"] += sinfo["checked"]
                stats["second_disagreed"] += sinfo["disagreed"]

        # ---- image
        image_info: dict = {}
        if image:
            img = open_rgb(image)
            image_info["size"] = list(img.size)
            photo = looks_like_photo(img)
            pf, pinfo = check_palette(kit, img, photo)
            findings += pf
            image_info.update(pinfo)
            read = None
            if fut_image:
                read, iinfo = fut_image.result()
                ai_calls.append(iinfo)
            if read:
                image_info["lines"] = read["lines"]
                cf, measured = check_text_contrast(kit, img, read["lines"])
                findings += cf
                image_info["contrast"] = measured
                image_text = " ".join(l["text"] for l in read["lines"])
                if image_text.strip():
                    icf, _ = claim_findings(kit, image_text, None, meta.get("locked") or [], target="image",
                                            line_boxes=read["lines"])
                    findings += [f for f in icf if f["verdict"] != "supported"]
            else:
                notes.append("Text inside the image was not read (AI unavailable), so image contrast and image "
                             "claims were not checked. Colour checks still ran.")
            findings += check_alt_text(kit, True, alt, read["alt_text"] if read else None)
            if read and read.get("alt_text") and (alt or "").strip():
                image_info["ai_alt"] = read["alt_text"]

    redacted: dict[str, int] = {}
    for c in ai_calls:
        for kind, n in (c.get("redacted") or {}).items():
            redacted[kind] = max(redacted.get(kind, 0), n)

    stats["needs_human"] += sum(1 for f in findings if f.get("needs_human") and f["method"] != "ai")
    findings = _order(findings)
    summary = _summary(findings, has_text, bool(image))
    summary.update({
        "ai": {"enabled": use_ai, "label": ai.label, "calls": ai_calls},
        "redacted": redacted,
        "stats": stats,
        "notes": notes,
        "image": image_info,
        "ms": int((time.time() - t0) * 1000),
        "kit_hash": kit.hash,
    })
    return {"findings": findings, "summary": summary}


def _order(findings: list[dict]) -> list[dict]:
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    target_rank = {"text": 0, "draft": 1, "image": 2, "alt": 3}
    findings.sort(key=lambda f: (rank.get(f["severity"], 9), target_rank.get(f["target"], 9),
                                 f["start"] if f["start"] is not None else 10**9))
    seen, out = set(), []
    n = 0
    for f in findings:
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        if f["severity"] != "ok":
            n += 1
            f["n"] = n
        else:
            f["n"] = None
        out.append(f)
    return out


def _summary(findings: list[dict], has_text: bool, has_image: bool) -> dict:
    counts = {s: 0 for s in SEVERITIES}
    dims = {d: {"issues": 0, "score": 100} for d in DIMENSIONS}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        d = dims.setdefault(f["dimension"], {"issues": 0, "score": 100})
        if f["severity"] != "ok":
            d["issues"] += 1
            d["score"] = max(0, d["score"] - PENALTY.get(f["severity"], 0))
    if not has_image:
        dims["visual"]["score"] = None
    if not has_text:
        for d in ("tone", "terminology", "messaging"):
            dims[d]["score"] = None
    claims_found = sum(1 for f in findings if f["dimension"] == "claims")
    return {
        "counts": counts,
        "dimensions": dims,
        "claims_found": claims_found,
        "issues": sum(v for k, v in counts.items() if k not in ("ok",)),
    }
