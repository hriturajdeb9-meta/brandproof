# Brandproof

**A pre-publication checkpoint for brand content.** Brandproof checks a draft against the brand's rules and a ledger of approved facts *before* it is published. It explains every finding with its source, suggests a fix that keeps the creator's intent, and sends only risky drafts to a human reviewer.

Built by **Team dibbankur** for Round 2 of the TechCircuit Hackathon 2026 (problem statement: *Intelligent brand-governance platform*). The demo brand, **Paisaleaf**, is a fictional savings app.

- **Live demo:** _add your Render link here_
- **No sign-in needed:** switch between Priya (Creator), Meera (Reviewer) and Arjun (Brand manager) at the top right.
- **Fastest tour:** click **Check the sample Instagram post** on the home page.

---

## What works (Round 2)

| Area | What you can do |
|---|---|
| **Brand Kit** | Rules (ID, dimension, severity, do/don't example, source page, optional regex), glossary, **Claim Ledger** (facts with evidence, owner and valid-until date) and colour palette. Add, edit, delete, CSV import and export. Changes apply to the next check. |
| **Content intake** | Paste text, upload a **.docx**, upload a **PNG/JPG/WebP** image (or text and image together, like a social post). Add a one-line goal and locked phrases that fixes must keep. |
| **Six checks** | **Facts & claims** (AI finds claims, code compares numbers and expiry dates with the ledger), **tone** (code patterns + AI against tone rules), **terminology** (glossary), **messaging** (banned phrases, required disclaimer), **visual** (image colours vs palette, CIEDE2000), **accessibility** (text contrast in images, alt text, readability, link text, inclusive language). |
| **Explainable findings** | Every finding shows the rule, the source page, the ledger fact (owner, evidence, expiry), the method (*Code check*, *AI, verified by code*, *AI-assessed*), confidence and a fix. Highlights in the text and boxes on the image link to the findings. |
| **Fixes** | Accept, edit or reject (with a reason) each finding. Accepted fixes go into a working copy; findings inside replaced text are resolved automatically. Re-check creates a new version. Locked phrases can never be removed by a fix. |
| **Review workflow** | Submit routes the draft: claims, rejected serious findings, open major findings or AI uncertainty → reviewer; otherwise auto-approved by policy. Reviewer approves or requests changes (comment required). Comments, version history with diffs, approval record. |
| **Team page** | The brand manager can rename the three demo people and change their titles; the new names show everywhere at once. |
| **Audit log** | Every check, fix, rejection, submission, approval and Brand Kit change, with who and when. |
| **Transparency** | AI status shown on every page, "How it works" page with AI disclosure, per-call logging, and an accuracy report from our test set. |
| **Resilience** | If the AI is missing, slow or rate-limited, all code checks still run and the page says what was skipped. AI answers are cached so the same draft gives the same report. |

## How a check works

```
draft (text / .docx / image)
  │
  ├─ 1. Read ─────────── text blocks; image resized, colours measured; AI reads image text + positions
  ├─ 2. Code checks ──── glossary, banned phrases, disclaimers, caps/emoji, readability, palette, contrast
  ├─ 3. AI checks ────── claims (+ which ledger fact each is about), tone vs tone rules   [in parallel]
  ├─ 4. Verify (code) ── AI quote must exist in the draft; rule ID must exist; numbers and expiry
  │                      compared with the ledger BY CODE; AI rewrites must keep locked phrases and
  │                      use only ledger numbers; low confidence → "needs human judgment"
  └─ 5. Explain ──────── findings with rule, source, method, confidence and fix → saved as a version
```

The AI never decides a claim's verdict on its own, and it never changes, approves or publishes anything.

## Architecture

- **One Python web service:** [Starlette](https://www.starlette.io/) (the framework FastAPI is built on) with server-rendered Jinja2 pages, plain CSS and a few lines of JavaScript. Every action works as a normal HTML form.
- **Database:** PostgreSQL when `DATABASE_URL` is set (we use Supabase), otherwise a local SQLite file. All SQL runs on both.
- **AI gateway:** `app/ai.py` is the only module that calls an AI provider. Google Gemini by default (`gemini-2.5-flash`), or any OpenAI-compatible API. It logs every call, caches answers by input hash and limits calls per minute to protect the free quota.
- **Image analysis:** Pillow. Colours are measured with median-cut grouping and the most common real pixel colour, compared with the palette using CIEDE2000, and contrast is measured with the WCAG formula.

```
app/
  main.py            routes, workflow, Brand Kit editing
  db.py              PostgreSQL / SQLite layer and schema
  seed.py            demo Brand Kit (Paisaleaf, fictional)
  kit.py             loads the Brand Kit for a check
  ai.py              LLM gateway (Gemini / OpenAI-compatible / fake for tests)
  colour.py          WCAG contrast and CIEDE2000
  textutil.py        sentence splitting, quote finding, readability, diffs
  checks/
    pipeline.py      runs every check, verifies AI output, builds the summary
    code_checks.py   glossary, pattern rules, disclaimers, caps/emoji, readability, alt text
    claims.py        claim detection and ledger verdicts (code decides)
    ai_checks.py     the three AI prompts and their validation
    image.py         palette and contrast checks
  templates/, static/
tests/               unit tests, HTTP workflow tests, browser end-to-end test, fake AI answers
scripts/eval.py      accuracy on data/testset.json
```

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY=...        # optional; without it Brandproof runs code checks only
uvicorn app.main:app --reload
# open http://localhost:8000
```

## Tests and accuracy

```bash
python -m unittest discover -s tests -v       # 21 unit + HTTP workflow tests (no AI key needed)
python tests/e2e_flow.py http://localhost:8000 # full browser click-through (needs Playwright)
python scripts/eval.py                         # accuracy on 30 labelled drafts → data/eval_results.json
```

The tests include a **fake AI** that returns deliberately bad answers (made-up quotes, a rule that doesn't exist, a rewrite with a wrong interest rate, a prompt-injection draft) to prove that the verification code catches them.

**Accuracy, code checks only (no AI):** 100% of findings correct, 30 of 34 planted errors caught (88%). The 4 misses are the tone and meaning issues that need the AI (jargon, mocking tone, "instant" vs "1 working day"). The test set is small and was written by the team, so these numbers show the checks work as designed; they are not an independent benchmark. Run `scripts/eval.py` with a Gemini key to measure the AI checks.

## Deploy

See **[DEPLOY.md](DEPLOY.md)**: Render (free) + Supabase (free) + Gemini (free tier), about 15 minutes.

## Disclosures

- **AI assistance:** an AI coding assistant (Claude, by Anthropic) was used to write much of this code and documentation during the Round 2 build window, under the team's direction. The team reviewed, tested, deployed and is responsible for it. Commits made with the assistant carry a `Co-Authored-By` trailer.
- **Third-party AI at runtime:** Google Gemini API (or another configured provider) receives the draft text, goal, locked phrases, tone rules, Claim Ledger facts and uploaded images, and nothing else. See the in-app *How it works* page.
- **Open-source libraries:** Starlette, Uvicorn, Jinja2, MarkupSafe, python-multipart, Pillow, python-docx, Requests, psycopg (see `requirements.txt` for versions and their licences). No project template or boilerplate generator was used.
- **Fonts and assets:** the UI uses the system font stack (Inter if installed). The sample banner and documents were generated by us for the fictional Paisaleaf brand. Mailchimp's public style guide is cited in our plan only as an example of subjective voice rules.

## Team

| Member | Area |
|---|---|
| Dibbankur Dey (lead) | AI checks, claims and verification; planning and submissions |
| Arnab Roy Acharjee | Backend, database, workflow and deployment |
| Hrituraj Deb | Frontend and UX |
| Sanarjit Das | Code checks, test set, user testing and documentation |

## Licence

MIT. See [LICENSE](LICENSE).
