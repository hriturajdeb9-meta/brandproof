"""Builds the sample PDFs used by the demo (run once; the output is committed in app/static/samples/).

  python scripts/make_samples.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

from app.record import _fonts  # noqa: E402

OUT = os.path.join(ROOT, "app", "static", "samples")
GREEN = colors.HexColor("#11734F")


def styles():
    _fonts()
    body = ParagraphStyle("b", fontName="BP", fontSize=10.5, leading=15, textColor=colors.HexColor("#12263F"))
    return {
        "body": body,
        "h1": ParagraphStyle("h1", parent=body, fontName="BP-Bold", fontSize=22, leading=27, textColor=GREEN, spaceAfter=8),
        "h2": ParagraphStyle("h2", parent=body, fontName="BP-Bold", fontSize=14, leading=18, spaceBefore=6, spaceAfter=6),
        "small": ParagraphStyle("s", parent=body, fontSize=8.5, leading=11, textColor=colors.HexColor("#5F6A82")),
        "li": ParagraphStyle("li", parent=body, leftIndent=12, bulletIndent=2, spaceAfter=3),
    }


def build(path, story, title):
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=20 * mm,
                            bottomMargin=20 * mm, title=title, author="Paisaleaf (fictional demo brand)")

    def footer(c, d):
        c.saveState()
        c.setFont("BP", 8)
        c.setFillColor(colors.HexColor("#5F6A82"))
        c.drawString(22 * mm, 10 * mm, f"{title} · fictional demo document for Brandproof")
        c.drawRightString(A4[0] - 22 * mm, 10 * mm, f"p. {d.page}")
        c.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def guideline():
    s = styles()
    P = lambda t, k="body": Paragraph(t, s[k])  # noqa: E731
    L = lambda t: Paragraph(t, s["li"], bulletText="•")  # noqa: E731
    story = [
        P("Paisaleaf Brand Guidelines", "h1"), P("Version 4 · October 2026 · Fictional demo brand", "small"), Spacer(1, 10),
        P("1. Who we are", "h2"),
        P("Paisaleaf helps first-time savers in India build a savings habit, one small step at a time. This guide "
          "tells everyone who writes or designs for Paisaleaf how to sound and look like us."),
        P("Every piece of content is checked against this guide before it is published."),
        PageBreak(),
        P("2. Voice and tone", "h2"),
        P("We sound like a calm, knowledgeable friend."),
        L("Never pressure people into money decisions. Do not use urgency phrases such as hurry, act now, last chance or don't miss out."),
        L("Use plain words. Explain any money term, such as interest or fixed deposit, the first time you use it."),
        L("Never mock or shame people for how they save, spend or earn."),
        L("Write in sentence case. Do not write whole words in capital letters in money messages."),
        L("Do not use slang such as lit, epic or OP in any channel."),
        PageBreak(),
        P("3. Names and words", "h2"),
        L("Write Paisaleaf as one word with a capital P, not Paisa Leaf."),
        L("Write Paisaleaf Save, not Paisaleaf Savings."),
        L("Call the people we serve savers, not customers."),
        L("Write email, not e-mail."),
        L("Say withdraw, not encash."),
        PageBreak(),
        P("4. Claims and promises", "h2"),
        L("Never promise guaranteed, assured or risk-free returns."),
        L("Every rate, number or rating must match the current approved fact sheet."),
        L("Words like best, number one or fastest need evidence that says exactly that."),
        L("Any message about rates or returns must include the disclaimer: *Rates are subject to change. T&amp;Cs apply."),
        L("Do not compare Paisaleaf with a named bank or competitor."),
        PageBreak(),
        P("5. Colour and imagery", "h2"),
        P("Use only the brand palette in graphics:"),
        Table([["Leaf Green", "#11734F"], ["Deep Ink", "#12263F"], ["Harvest Amber", "#B45309"], ["Mint", "#E8F3EE"],
               ["Cream", "#FBFAF6"]], colWidths=[50 * mm, 40 * mm],
              style=TableStyle([("FONTNAME", (0, 0), (-1, -1), "BP"), ("FONTSIZE", (0, 0), (-1, -1), 10),
                                ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDE2EC"))])),
        Spacer(1, 6),
        L("Text in images must have a contrast of at least 4.5:1 with its background."),
        L("Show real, diverse people. Never use images of piles of cash or gold."),
        PageBreak(),
        P("6. Accessibility and privacy", "h2"),
        L("Every image needs alt text that describes it and any words in it."),
        L("Link text must say where the link goes; never use click here on its own."),
        L("Never show real account numbers, PAN or Aadhaar numbers, even as examples."),
        L("Do not mention unannounced products or internal codenames such as Project Neem."),
    ]
    build(os.path.join(OUT, "paisaleaf-brand-guidelines.pdf"), story, "Paisaleaf Brand Guidelines v4")


def brochure():
    s = styles()
    P = lambda t, k="body": Paragraph(t, s[k])  # noqa: E731
    story = [
        P("Paisaleaf Save", "h1"),
        P("Save a little, every day. Start saving with just ₹100 and watch your money grow."),
        P("Paisaleaf Save pays up to 7.25% p.a., the best rate in India. Your interest is added every month."),
        PageBreak(),
        P("Why savers choose us", "h2"),
        P("Your deposits are insured up to ₹5 lakh by DICGC. Withdraw anytime: money reaches your bank within 1 working day."),
        P("Hurry! This offer won't last. Over 5 lakh customers already trust Paisaleaf."),
        PageBreak(),
        P("Get started", "h2"),
        P("Download the app and signup today. Questions? Send an e-mail to help@paisaleaf.example or click here."),
    ]
    build(os.path.join(OUT, "paisaleaf-brochure.pdf"), story, "Paisaleaf Save brochure")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    guideline()
    brochure()
    print("wrote", os.listdir(OUT))
