"""Exportable approval record (PDF) for an approved draft: what was approved, by whom, under which checks,
which findings were fixed or overridden (with reasons), and the full activity trail. A SHA-256 fingerprint of
the approved content lets an auditor confirm that what was published is what was approved."""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

from .web import when

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_fonts_ready = False
INK = colors.HexColor("#13213C")
MUTED = colors.HexColor("#5F6A82")
LINE = colors.HexColor("#D9DEE8")
GREEN = colors.HexColor("#0E8A6A")
SEV = {"blocker": "#B42318", "major": "#B54708", "minor": "#475467", "info": "#475467"}
ACTIONS = {"accept": "Fix accepted", "edit": "Fix edited and accepted", "reject": "Rejected with reason",
           "acknowledge": "To be fixed in the design", "superseded": "Resolved by another fix"}


def _fonts():
    global _fonts_ready
    if not _fonts_ready:
        pdfmetrics.registerFont(TTFont("BP", os.path.join(FONT_DIR, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("BP-Bold", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("BP", normal="BP", bold="BP-Bold", italic="BP", boldItalic="BP-Bold")
        _fonts_ready = True


def _styles():
    base = ParagraphStyle("b", fontName="BP", fontSize=9, leading=12.5, textColor=INK, alignment=TA_LEFT)
    return {
        "body": base,
        "small": ParagraphStyle("s", parent=base, fontSize=7.8, leading=10.5, textColor=MUTED),
        "h1": ParagraphStyle("h1", parent=base, fontName="BP-Bold", fontSize=17, leading=21, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=base, fontName="BP-Bold", fontSize=11, leading=14, spaceBefore=10, spaceAfter=4),
        "cell": ParagraphStyle("c", parent=base, fontSize=8, leading=10.5),
        "cellb": ParagraphStyle("cb", parent=base, fontName="BP-Bold", fontSize=8, leading=10.5),
        "quote": ParagraphStyle("q", parent=base, fontSize=9.5, leading=13.5, leftIndent=6, borderPadding=6,
                                backColor=colors.HexColor("#F6F7FA")),
    }


def P(text, style) -> Paragraph:
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def fingerprint(body: str, image: bytes | None) -> str:
    h = hashlib.sha256((body or "").encode("utf-8"))
    if image:
        h.update(b"\x00image\x00")
        h.update(image)
    return h.hexdigest()


def build_record(workspace: str, draft: dict, version: dict, decisions: dict, audit: list[dict], comments: list[dict],
                 image: bytes | None) -> bytes:
    _fonts()
    st = _styles()
    buf = io.BytesIO()
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("BP", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, f"Brandproof approval record · draft #{draft['id']} v{version['n']} · generated {when(generated)} IST")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=f"Approval record: {draft['title']}", author="Brandproof")
    summary = version.get("summary_obj") or {}
    findings = [f for f in version.get("findings_list") or [] if f["severity"] != "ok"]
    supported = [f for f in version.get("findings_list") or [] if f["severity"] == "ok"]
    approval = next((a for a in reversed(audit) if a["action"] in ("review.approved", "draft.auto_approved")), None)
    adetail = json.loads(approval.get("detail") or "{}") if approval else {}
    story = []
    story.append(P("APPROVAL RECORD", ParagraphStyle("k", parent=st["small"], fontName="BP-Bold", textColor=GREEN)))
    story.append(P(draft["title"], st["h1"]))
    story.append(P(f"{workspace} · {draft.get('channel') or ''} · draft #{draft['id']}, version {version['n']} · "
                   f"created by {draft.get('created_by') or ''}", st["small"]))
    story.append(Spacer(1, 6))

    if approval and approval["action"] == "draft.auto_approved":
        decision = f"Cleared automatically by policy: {adetail.get('policy', '')}"
    elif approval:
        decision = f"Approved by {approval['actor']} ({approval['role']})"
    else:
        decision = "Approved"
    rows = [
        ["Decision", decision],
        ["Approved at", f"{when(approval['created_at'])} IST" if approval else ""],
        ["Reviewer comment", adetail.get("comment") or "—"],
        ["Content fingerprint", f"SHA-256 {fingerprint(version.get('body') or '', image)}"],
        ["Checks", f"{summary.get('issues', 0)} open finding(s) at approval · {len(supported)} claim(s) verified against "
                   f"the Claim Ledger · Brand Kit version {version.get('kit_hash') or ''}"],
        ["AI used", _ai_line(summary)],
    ]
    masked = (summary.get("masked") or {})
    if masked.get("total"):
        rows.append(["Privacy", "Masked before any AI call: " + ", ".join(masked.get("labels") or [])])
    t = Table([[P(a, st["cellb"]), P(b, st["cell"])] for a, b in rows], colWidths=[36 * mm, 138 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(t)

    story.append(P("Approved content", st["h2"]))
    if image:
        try:
            from PIL import Image as PILImage
            im = PILImage.open(io.BytesIO(image))
            w, h = im.size
            width = min(120 * mm, 174 * mm)
            story.append(Image(io.BytesIO(image), width=width, height=width * h / w))
            story.append(P(f"Alt text: {version.get('alt_text') or '(none)'}", st["small"]))
            story.append(Spacer(1, 4))
        except Exception:
            story.append(P("(image could not be embedded)", st["small"]))
    if version.get("body"):
        story.append(P(version["body"], st["quote"]))
    if version.get("source_name"):
        story.append(P(f"Source file: {version['source_name']}", st["small"]))

    story.append(P("Findings and what was decided", st["h2"]))
    if findings:
        head = [P(x, st["cellb"]) for x in ("#", "Severity", "Rule", "Finding", "Decision")]
        data = [head]
        for f in findings:
            d = decisions.get(f["key"])
            if d:
                dec = ACTIONS.get(d["action"], d["action"]) + (f": “{d['reason']}”" if d.get("reason") and d["action"] == "reject" else "")
                dec += f" — {d['actor']}"
            else:
                dec = "Open at approval"
            extra = ""
            if f.get("second_opinion"):
                so = f["second_opinion"]
                extra = " Second opinion: " + ("agreed." if so.get("agree") else "did not confirm; reviewed by a person.")
            data.append([P(f.get("n") or "", st["cell"]),
                         Paragraph(f'<font color="{SEV.get(f["severity"], "#475467")}"><b>{f["severity"]}</b></font>', st["cell"]),
                         P(f["rule_id"], st["cell"]),
                         P(f"{f['title']}" + (f" — “{f['quote']}”" if f.get('quote') and f['quote'] not in f['title'] else "") + extra, st["cell"]),
                         P(dec, st["cell"])])
        t = Table(data, colWidths=[8 * mm, 16 * mm, 20 * mm, 78 * mm, 52 * mm], repeatRows=1)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F3F7")),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
    else:
        story.append(P("No open findings in the approved version.", st["body"]))
    if supported:
        story.append(Spacer(1, 4))
        story.append(P("Verified claims: " + "; ".join(
            f"“{f['quote']}” ({', '.join(e.get('id', '') for e in f.get('evidence') or [] if isinstance(e, dict) and e.get('id'))})"
            for f in supported), st["small"]))

    if comments:
        story.append(P("Comments", st["h2"]))
        for cm in comments:
            story.append(P(f"{when(cm['created_at'])} · {cm['actor']} ({cm['role']}): {cm['body']}", st["cell"]))

    story.append(P("Activity trail", st["h2"]))
    trail = [[P(when(a["created_at"]), st["cell"]), P(a["actor"], st["cell"]), P(_act(a), st["cell"])] for a in audit]
    t = Table([[P(x, st["cellb"]) for x in ("When (IST)", "Who", "What")]] + trail, colWidths=[32 * mm, 28 * mm, 114 * mm],
              repeatRows=1)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F3F7")),
                           ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story.append(t)
    story.append(Spacer(1, 8))
    story.append(KeepTogether([P(
        "This record was generated by Brandproof from its append-only audit log. AI findings are suggestions checked "
        "by code; every fix and every approval was made by a person. To verify the published content, compute the "
        "SHA-256 of its text (and image) and compare it with the fingerprint above.", st["small"])]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def _ai_line(summary: dict) -> str:
    ai = summary.get("ai") or {}
    if not ai.get("enabled"):
        return "AI off (code checks only)"
    used = sorted({f"{c.get('provider')}:{c.get('model')}" for c in ai.get("calls") or [] if c.get("ok")})
    so = summary.get("second_opinion") or {}
    line = ", ".join(used) or ai.get("label", "")
    if so.get("checked"):
        line += f" · second opinion on {so['checked']} serious finding(s): {so.get('agreed', 0)} agreed"
    return line


def _act(a: dict) -> str:
    from .web import activity
    d = json.loads(a.get("detail") or "{}")
    text = activity(a["action"])
    if d.get("version"):
        text += f" (v{d['version']})"
    if d.get("reason"):
        text += f": “{d['reason']}”"
    elif d.get("comment"):
        text += f": “{d['comment']}”"
    elif d.get("routed_because"):
        text += ": " + "; ".join(d["routed_because"])
    return text
