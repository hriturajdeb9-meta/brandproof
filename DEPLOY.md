# Deploying Brandproof (about 10 minutes, free)

You need: a **GitHub** account (code), a **Render** account (hosting, sign in with GitHub) and a **Gemini API key** (free tier). A database account is optional.

## 1. Code on GitHub
Create a **public** repository and upload this folder (or `git push` it).

## 2. Gemini API key
<https://aistudio.google.com/apikey> → **Create API key** → copy it. Never commit it or paste it in chat; it goes only into Render.

## 3. Render web service
1. <https://render.com> → **New + → Web Service** → pick the repository.
2. **Runtime:** Python 3 · **Build command:** `pip install -r requirements.txt`
3. **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
4. **Instance type:** Free · **Health check path:** `/healthz`
5. **Environment variables:** `GEMINI_API_KEY` = your key, `GEMINI_MODEL` = `gemini-flash-latest`, `PYTHON_VERSION` = `3.13.5`
6. **Create Web Service**, wait for "Live".

(Or **New + → Blueprint** and pick the repo: `render.yaml` fills in everything except the key.)

The app keeps itself awake on Render's free plan (it pings its own `/healthz` every 10 minutes; set `KEEP_ALIVE=0` to turn that off).

## 4. Check it
- `https://<your-app>.onrender.com/healthz` should show `"ok": true` and name Gemini under `"ai"`.
- On the home page click **Check the sample Instagram post**. The "How this check ran" box should say the AI calls succeeded and the banner should show numbered boxes around the orange text.
- If the AI calls fail with "model not found", set `GEMINI_MODEL=gemini-2.5-flash` and redeploy.

## Optional: permanent storage (Supabase)
By default data lives in a SQLite file on the Render server, which is reset when the service is redeployed or restarted (the demo Brand Kit is recreated automatically). To keep data permanently:
1. <https://supabase.com> → **New project** (password with letters and numbers only).
2. **Connect → Session pooler** → copy the URI and replace `[YOUR-PASSWORD]`.
3. Add it in Render as `DATABASE_URL` and redeploy. `/healthz` then shows `"storage": "postgres"`.

## Troubleshooting
| Problem | Fix |
|---|---|
| Build fails on a package version | Set `PYTHON_VERSION` to `3.13.5` (or another 3.13.x) in Render. |
| AI calls fail with HTTP 429 | Free-tier rate limit. Wait a minute; answers are cached and calls are capped at 12 per minute. |
| AI calls fail with HTTP 400/404 | Set `GEMINI_MODEL=gemini-2.5-flash`. |
| `storage: sqlite` with a warning | Only if you set `DATABASE_URL`: re-copy the Session pooler string. |
