# Deploying Brandproof (about 15 minutes, all free)

You need four free accounts: **GitHub**, **Google AI Studio** (Gemini key), **Supabase** (database) and **Render** (hosting). Recommended: **Groq** (backup AI and the independent second opinion). Optional: **UptimeRobot**, to stop the free Render server from sleeping.

## 1. Put the code on GitHub

1. Create a new **public** repository called `brandproof` (no README, no .gitignore, empty).
2. In the folder that contains this README:
   ```bash
   git remote add origin https://github.com/<your-username>/brandproof.git
   git branch -M main
   git push -u origin main
   ```

## 2. Get a Gemini API key

1. Go to <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** and copy it. **Never commit it or paste it in chat.** It goes only into Render's settings.

### Recommended: a Groq API key

Brandproof uses Groq as the backup when Gemini is busy or out of quota, and as the **independent second opinion** on AI-only findings. Without a Groq key, the second opinion falls back to Gemini, which is less independent and uses up your Gemini quota faster.

1. Go to <https://console.groq.com/keys>, sign in, and click **Create API key**.
2. Copy it. Like the Gemini key, it goes only into Render's settings.

## 3. Create the database (Supabase)

1. <https://supabase.com> → **New project**. Choose the region **Mumbai (ap-south-1)** if offered.
2. Set a database password **using only letters and numbers** (special characters need URL-encoding). Save it.
3. When the project is ready, click **Connect** (top of the dashboard) → **Connection string** → choose **Session pooler**.
4. Copy the URI. It looks like:
   `postgresql://postgres.abcdefghijk:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`
5. Replace `[YOUR-PASSWORD]` (including the brackets) with your password. This is your `DATABASE_URL`.

Brandproof creates its tables, turns on row-level security and loads the demo workspace by itself on first start. You don't need to run any SQL.

> **Upgrading from Round 2?** You can keep the same database. On the first start, Brandproof renames the Round 2 tables to `r2_*` (nothing is deleted), creates the new ones and loads the demo workspace again. `/healthz` shows a one-time warning that says so.

> Use the **Session pooler** string, not "Direct connection": the direct one needs IPv6, which Render's free plan doesn't support.

## 4. Deploy on Render

1. <https://render.com> → sign in with GitHub → **New +** → **Web Service** → pick the `brandproof` repo.
2. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
   - **Instance type:** Free
3. **Environment variables** (Advanced → Add environment variable):
   | Key | Value | |
   |---|---|---|
   | `PYTHON_VERSION` | `3.13.5` | required |
   | `GEMINI_API_KEY` | your key from step 2 | required for AI checks |
   | `DATABASE_URL` | your Supabase URI from step 3 | required; without it data is lost on every restart |
   | `GROQ_API_KEY` | your Groq key from step 2 | recommended |
   | `SESSION_SECRET` | any long random string | optional; otherwise one is generated and kept in the database |

   Leave these **unset** unless you want them:
   - `SUPABASE_URL` and `SUPABASE_ANON_KEY` switch sign-in from Brandproof's built-in accounts to Supabase Auth (email and password). Find them in Supabase under **Project Settings → API**. Use the public **anon** key, never the service-role key.
   - `GEMINI_MODEL` is an older setting. If it is set, that model is tried first on every AI call. **If you deployed Round 2, delete it** (or set it to `gemini-3.5-flash`): the Round 2 value `gemini-2.5-flash` is retired, so every call would waste a request on it before falling back.
   - `AI_ROUTE` and `AI_ROUTE_<JOB>` choose the models per job; see `.env.example`. The defaults are fine.
4. **Health check path:** `/healthz`
5. Click **Create Web Service** and wait for "Live" (3–5 minutes). Your link is `https://brandproof-xxxx.onrender.com`.

(Alternative: **New + → Blueprint** and pick the repo. `render.yaml` fills in everything else and asks for the secret values; leave the two Supabase Auth ones empty unless you want Supabase sign-in.)

## 5. Check it works

Open `https://<your-app>.onrender.com/healthz`. You should see:
```json
{"ok": true, "storage": "postgres", "rls": true, "ai": "Google Gemini · gemini-3.5-flash", "auth": "built-in", "warning": ""}
```
- `storage` must be **postgres**. If it says sqlite with a warning, the `DATABASE_URL` is wrong (check the password and that you used the Session pooler string).
- `rls` should be **true**: the database itself enforces workspace membership and roles. If it is false, the app still enforces roles and `warning` says why.
- `ai` must name an AI provider. If it says "AI off", the key variable name is wrong.
- `auth` is `built-in`, or `supabase` if you set the two Supabase Auth variables.

Then on the home page click **Check the sample Instagram post**. The "How this check ran" box on the right should say the AI calls succeeded, and the banner should show numbered boxes around the orange text (contrast findings).

If a model is busy, retired or out of quota, Brandproof moves on to the next one: `gemini-3.5-flash`, then `gemini-3.5-flash-lite`, then `gemini-flash-latest`, then Groq (if `GROQ_API_KEY` is set). Change the order with `AI_ROUTE` (see `.env.example`).

## 6. Keep it awake for the judges (important)

Free Render servers go to sleep after 15 minutes without visitors, and the first visit then takes about a minute. To avoid that:

1. <https://uptimerobot.com> → free account → **Add New Monitor**.
2. Type **HTTP(s)**, URL `https://<your-app>.onrender.com/healthz`, interval **5 minutes**.

## 7. Before you submit

- Open the live link in an **incognito window** and click through: sample post → accept a few fixes → Re-check → Submit → switch to Meera → Approve.
- Open the GitHub repo link in an incognito window too. It must be **public**.
- Optional: run `python scripts/eval.py` on a laptop with `GEMINI_API_KEY` (and `GROQ_API_KEY`) set to measure accuracy with AI, then commit `data/eval_results.json` so the numbers appear on the *How it works* page. It makes about 90 AI calls, so run it when your free quota is fresh.

## Troubleshooting

| Problem | Fix |
|---|---|
| Build fails on a package version | Make sure the service uses Python 3.13 (`.python-version` is in the repo). |
| `storage: sqlite` with a warning | `DATABASE_URL` is wrong. Re-copy the Session pooler string and replace `[YOUR-PASSWORD]`. |
| AI calls fail with HTTP 429 | Free-tier rate or daily limit. Wait a minute (Brandproof caches answers and limits itself to 12 Gemini calls a minute), or add `GROQ_API_KEY` so busy calls go to Groq. |
| Every AI check is slow, and Render's logs show HTTP 404 for `gemini-2.5-flash` | A Round 2 `GEMINI_MODEL` setting is still there. Delete it in Render's Environment settings. |
| Deploy fails with `column "workspace_id" does not exist` | You are running code from before the Round 2 upgrade step. Deploy the latest `main`. |
| AI calls fail with HTTP 400 | The API key is wrong. Create a new one and update `GEMINI_API_KEY`. |
| Page takes ~1 minute to open | The free server was asleep. Set up step 6. |
