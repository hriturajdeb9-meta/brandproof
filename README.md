# Brandproof

**A pre-publication checkpoint for brand content.** Paste a post, email, PDF or banner. Brandproof checks it against the brand's rules and a ledger of approved facts, explains every problem with its source, suggests a fix that keeps the creator's intent, and sends only risky content to a human reviewer.

Built by **Team DIBBANKUR** for the TechCircuit Hackathon 2026 (problem statement: *Intelligent brand-governance platform*). The demo brand, **Paisaleaf**, is a fictional savings app.

- **Live product:** <https://brandproof-wo96.onrender.com>. The free server sleeps; the first visit after a quiet spell can take about a minute.
- **No sign-in needed:** the link opens the public demo. Switch between **Priya** (creator), **Meera** (reviewer) and **Arjun** (brand manager) at the top.
- **Demo logins**, if you prefer signing in: `priya@demo.brandproof`, `meera@demo.brandproof`, `arjun@demo.brandproof`, password `demo2026`. Or create your own free workspace at `/signup`.

## Two-minute tour

1. On the home page click **▶ Run a live check**. The check runs in the background and the page fills in by itself.
2. Click **✨ Fix all & re-check**, then **🖼 Use the corrected banner & re-check**, then **Submit**.
3. Switch to **Meera** at the top, click **Approve**, and download the **approval record (PDF)**.
4. Switch to **Arjun**: open **More → Import a guideline**, upload [the sample guideline PDF](app/static/samples/paisaleaf-brand-guidelines.pdf) and approve the rules it drafts. Then open **Insights** (common mistakes, Clarity Radar, expiry watch).

## Features

| Area | What you can do |
|---|---|
| **Checks** | Six dimensions: **facts & claims** (AI finds claims, code compares numbers and expiry dates with the Claim Ledger), **tone**, **terminology** (glossary), **messaging** (banned phrases, required disclaimers, confidential terms, personal ID numbers), **visual** (image colours vs palette, CIEDE2000) and **accessibility** (text contrast in images, alt text, readability, link text, inclusive language). |
| **Input** | Pasted text, Word (.docx), **PDF (findings show their page)**, PNG/JPG/WebP images, or text plus an image. Optional goal and locked phrases that fixes must keep. |
| **Explainable findings** | Each finding shows the problem, the suggested fix and, under *Why?*, the rule, its source page, the ledger fact (owner, evidence, expiry), how it was found (code / AI verified by code / AI-assessed), confidence and the second opinion. |
| **Fixing** | Accept, edit or reject (with a reason) each finding, or **Fix all & re-check** in one click. Re-check makes a new version; rejections carry over; locked phrases are never removed. |
| **Workflow** | Risk-based routing: claims, rejected serious findings, open majors or AI uncertainty go to a reviewer; clean drafts are approved automatically. Reviewer approves or requests changes. Comments, version history with diffs, append-only audit log. **PDF approval record** with a SHA-256 fingerprint of the approved content. |
| **Guideline import** *(improvement 1)* | Upload a brand guideline (PDF, Word or text). AI drafts atomic rules (dimension, severity, do/don't, page, exact quote); **code checks each quote is really on that page**; banned words become tested code patterns; near-duplicates are flagged; colour codes become palette entries. The brand manager approves, edits or discards each one. Rules are **versioned** and their examples run as **tests**. |
| **Insights** *(improvement 2)* | Most common mistakes by rule, dimension, channel and creator, with a weekly trend. **Guideline Clarity Radar** ranks rules people reject or the AI is unsure about, and the AI drafts clearer wording to approve as a new rule version. **Expiry watch** lists approved content whose facts expire soon, with one-click reopen. |
| **Trust & control** *(improvement 3)* | Real accounts and workspaces (built-in or Supabase Auth). **Roles enforced by PostgreSQL row-level security**: the database refuses a reviewer editing the Brand Kit and anyone editing the audit log. Emails, phone, Aadhaar, PAN, card numbers and confidential terms are **masked before any AI call**. Serious AI findings get a **second opinion** from a different model; disagreement means a person decides. |
| **Reliability** | Checks run in the background (pages never freeze) and resume after a restart. Double clicks and refreshes never create duplicates. Each AI job has a list of models: if Gemini is busy, the next model answers (Groq as backup). If all AI is down, every code check still runs and the page says what was skipped. |

## How a check works

```
draft (text / Word / PDF / image)
  │
  ├─ 1. Read ─────────── text and pages; image resized, colours measured; personal data and confidential terms masked
  ├─ 2. Code checks ──── glossary, banned phrases, disclaimers, caps/emoji, readability, confidential terms, palette, contrast
  ├─ 3. AI checks ────── claims (+ which ledger fact), tone/voice rules (most relevant rules if the kit is large),
  │                      text in the image                                            [in parallel, background job]
  ├─ 4. Verify (code) ── quote must exist; rule must exist; numbers and dates compared with the ledger BY CODE;
  │                      rewrites must keep locked phrases and use only ledger numbers
  ├─ 5. Second opinion ─ serious AI-dependent findings re-judged by another model; disagreement → needs a human
  └─ 6. Explain ──────── findings with rule, source, evidence, method, confidence and fix → saved as a version
```

The AI never decides a claim's verdict on its own, and it never changes, approves or publishes anything.

## Architecture

- **One Python web service:** [Starlette](https://www.starlette.io/) + Uvicorn, server-rendered Jinja2 pages, plain CSS and a little JavaScript. Every action is a normal HTML form, so it works without JavaScript too.
- **Database:** PostgreSQL when `DATABASE_URL` is set (we use Supabase), otherwise a local SQLite file. On PostgreSQL, [`app/rls.sql`](app/rls.sql) installs a restricted `bp_app` role and row-level-security policies; every request runs as that role with the user's id and workspace.
- **AI gateway:** [`app/ai.py`](app/ai.py) is the only module that calls AI. Per-job model routes (`AI_ROUTE_CLAIMS`, `AI_ROUTE_TONE`, `AI_ROUTE_IMAGE`, `AI_ROUTE_VERIFY`, `AI_ROUTE_IMPORT`, `AI_ROUTE_CLARIFY`), automatic fallback with cool-down, JSON output at temperature 0, answer cache, call log and per-provider rate limits. Defaults: Gemini 3.5 Flash → Flash-Lite → Groq `openai/gpt-oss-120b`; the second opinion starts with Groq so it is independent.
- **Background jobs:** a small thread pool ([`app/jobs.py`](app/jobs.py)); unfinished jobs are picked up again on start-up.

```
app/
  main.py            routes and start-up              web.py          sessions, page rendering, uploads
  routes_drafts.py   drafts, fixes, review, record    routes_kit.py   Brand Kit, rule versions and tests
  routes_account.py  sign-in, workspaces, team        auth.py         passwords, sessions, Supabase Auth
  guidelines.py      guideline import (improvement 1) insights.py     insights, Clarity Radar, expiry watch (2)
  redact.py          masking before AI (3)            record.py       PDF approval record (3)
  ai.py              AI gateway                       retrieval.py    keyword (BM25) rule selection
  db.py, rls.sql     database layer and security      jobs.py         background jobs
  seed.py, demo_history.py   demo workspace           checks/         the checking pipeline
tests/               unit, HTTP workflow and row-level-security tests, fake AI answers
scripts/eval.py      accuracy, speed and failures on data/testset.json
```

## Run it locally

Needs **Python 3.13** (3.11+ works).

```bash
git clone https://github.com/hriturajdeb9-meta/brandproof.git
cd brandproof
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # optional: add GEMINI_API_KEY / GROQ_API_KEY; empty = code checks only
uvicorn app.main:app --reload       # reads .env by itself
```

Open <http://localhost:8000>. With no settings at all it runs on a local SQLite file (`data/brandproof-v3.db`) with code checks only and built-in accounts; the demo workspace loads by itself. Add `DATABASE_URL` for PostgreSQL with row-level security. See [`.env.example`](.env.example) for every setting and [DEPLOY.md](DEPLOY.md) to put it online (Render + Supabase, free, about 15 minutes).

## Tests and accuracy

```bash
pip install -r requirements-dev.txt                   # test-only extras
python -m unittest discover -s tests -v               # unit, fake-AI and HTTP workflow tests; no AI key needed
TEST_DATABASE_URL=postgresql://... python -m unittest tests.test_round3   # adds the row-level-security test
python scripts/eval.py                                # accuracy + speed on 30 labelled drafts
python scripts/eval.py --route groq:openai/gpt-oss-120b --no-cache --out data/eval-groq.json   # compare models
```

The tests include a **fake AI** that gives deliberately bad answers (made-up quotes, a rule that doesn't exist, a wrong interest rate, a prompt-injection draft, a busy model) to prove the verification and fallback code catches them. GitHub Actions runs the suite on every push.

**Accuracy, code checks only:** 100% of findings correct, 30 of 34 planted errors caught (88%); the 4 misses need the AI (jargon, mocking tone, "instant" vs "1 working day"). The test set is small and written by the team, so it shows the checks work as designed; it is not an independent benchmark.

## Disclosures

- **AI assistance:** an AI coding assistant (Claude, by Anthropic) wrote much of this code and documentation under the team's direction, inside the event windows. The team reviewed, tested, deployed and is responsible for it. Commits made with the assistant carry a `Co-Authored-By` trailer.
- **AI at runtime:** Google Gemini and Groq receive the masked draft text, the relevant rules and ledger facts, uploaded images and imported guideline pages, and nothing else. See the in-app *How it works* page.
- **Open source:** Starlette, Uvicorn, Jinja2, MarkupSafe, python-multipart, Pillow, python-docx, pypdf, ReportLab, Requests, psycopg; DejaVu fonts for the PDF record (licence in `app/fonts`).
- **Demo data:** Paisaleaf, its rules, facts, people and sample history are fictional.

## Team

| Member | Area |
|---|---|
| Dibbankur Dey (lead) | AI checks and verification, guideline import; planning and submission |
| Arnab Roy Acharjee | Backend, database, sign-in, roles and privacy, deployment |
| Hrituraj Deb | Frontend, UX and design, insights |
| Sanarjit Das | Code checks, test set, testing and documentation |

## Licence

MIT. See [LICENSE](LICENSE).
