# Deploying Brandproof (about 15 minutes, all free)

You need four free accounts: **GitHub**, **Google AI Studio** (Gemini key), **Supabase** (database) and **Render** (hosting). Optional: **UptimeRobot**, to stop the free Render server from sleeping.

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

## 3. Create the database (Supabase)

1. <https://supabase.com> → **New project**. Choose the region **Mumbai (ap-south-1)** if offered.
2. Set a database password **using only letters and numbers** (special characters need URL-encoding). Save it.
3. When the project is ready, click **Connect** (top of the dashboard) → **Connection string** → choose **Session pooler**.
4. Copy the URI. It looks like:
   `postgresql://postgres.abcdefghijk:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`
5. Replace `[YOUR-PASSWORD]` (including the brackets) with your password. This is your `DATABASE_URL`.

Brandproof creates its tables and loads the demo Brand Kit by itself on first start. You don't need to run any SQL.

> Use the **Session pooler** string, not "Direct connection": the direct one needs IPv6, which Render's free plan doesn't support.

## 4. Deploy on Render

1. <https://render.com> → sign in with GitHub → **New +** → **Web Service** → pick the `brandproof` repo.
2. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
   - **Instance type:** Free
3. **Environment variables** (Advanced → Add environment variable):
   | Key | Value |
   |---|---|
   | `GEMINI_API_KEY` | your key from step 2 |
   | `DATABASE_URL` | your Supabase URI from step 3 |
   | `GEMINI_MODEL` | `gemini-2.5-flash` |
4. **Health check path:** `/healthz`
5. Click **Create Web Service** and wait for "Live" (3–5 minutes). Your link is `https://brandproof-xxxx.onrender.com`.

(Alternative: **New + → Blueprint** and pick the repo; `render.yaml` fills in everything except the two secrets.)

## 5. Check it works

Open `https://<your-app>.onrender.com/healthz`. You should see:
```json
{"ok": true, "storage": "postgres", "ai": "Google Gemini · gemini-2.5-flash", "warning": ""}
```
- `storage` must be **postgres**. If it says sqlite with a warning, the `DATABASE_URL` is wrong (check the password and that you used the Session pooler string).
- `ai` must name Gemini. If it says "AI off", the key variable name is wrong.

Then on the home page click **Check the sample Instagram post**. The "How this check ran" box on the right should say the AI calls succeeded, and the banner should show numbered boxes around the orange text (contrast findings).

If one Gemini model is busy or missing, Brandproof automatically tries `gemini-2.5-flash-lite` and then `gemini-flash-latest` (change the order with `GEMINI_FALLBACK_MODELS`).

## 6. Keep it awake for the judges (important)

Free Render servers go to sleep after 15 minutes without visitors, and the first visit then takes about a minute. To avoid that:

1. <https://uptimerobot.com> → free account → **Add New Monitor**.
2. Type **HTTP(s)**, URL `https://<your-app>.onrender.com/healthz`, interval **5 minutes**.

## 7. Before you submit

- Open the live link in an **incognito window** and click through: sample post → accept a few fixes → Re-check → Submit → switch to Meera → Approve.
- Open the GitHub repo link in an incognito window too. It must be **public**.
- Optional: run `python scripts/eval.py` on a laptop with `GEMINI_API_KEY` set to measure accuracy with AI, then commit `data/eval_results.json` so the numbers appear on the *How it works* page.

## Troubleshooting

| Problem | Fix |
|---|---|
| Build fails on a package version | Make sure the service uses Python 3.13 (`.python-version` is in the repo). |
| `storage: sqlite` with a warning | `DATABASE_URL` is wrong. Re-copy the Session pooler string and replace `[YOUR-PASSWORD]`. |
| AI calls fail with HTTP 429 | Free-tier rate limit. Wait a minute; Brandproof caches answers and limits itself to 12 AI calls a minute. |
| AI calls fail with HTTP 400 | The API key is wrong. Create a new one and update `GEMINI_API_KEY`. |
| Page takes ~1 minute to open | The free server was asleep. Set up step 6. |
