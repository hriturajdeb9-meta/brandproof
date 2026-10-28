"""LLM gateway: the only module that talks to an AI provider.

Every AI job (finding claims, reviewing tone, reading an image, drafting rules from a guideline, the
second-opinion check, ...) has a *route*: an ordered list of provider:model steps. The first step that
answers wins. A step that is busy (HTTP 429/5xx), too slow or missing is skipped and cooled down for a
minute, so the next call goes straight to the backup instead of waiting again.

Providers
  gemini  Google Gemini API            needs GEMINI_API_KEY
  groq    Groq (OpenAI-compatible)     needs GROQ_API_KEY
  openai  any OpenAI-compatible API    needs OPENAI_API_KEY (+ OPENAI_BASE_URL)
  fake    canned answers from FAKE_AI_FILE, used by the automated tests

Choosing models (no code changes needed)
  AI_ROUTE_<JOB>=gemini:gemini-2.5-flash,groq:openai/gpt-oss-120b   one job, e.g. AI_ROUTE_TONE
  AI_ROUTE=...                                                        every text job
  AI_ROUTE_IMAGE=...                                                  image reading
  AI_PROVIDER=none                                                    AI off (code checks only)
  AI_PROVIDER=fake                                                    tests
The brand manager can also change the route per job on the "How it works" page (stored in the database).
Steps whose API key is missing are skipped automatically.

Every call is logged to `ai_calls`; answers are cached by a hash of (job, route, prompt, image), so the
same draft with the same settings gives the same report.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque

import requests

from .db import Database, now_iso

log = logging.getLogger("brandproof.ai")

PROVIDERS = {
    "gemini": {"label": "Google Gemini", "key": "GEMINI_API_KEY"},
    "groq": {"label": "Groq", "key": "GROQ_API_KEY", "base": "https://api.groq.com/openai/v1"},
    "openai": {"label": "OpenAI-compatible", "key": "OPENAI_API_KEY", "base_env": "OPENAI_BASE_URL",
               "base": "https://api.openai.com/v1"},
    "fake": {"label": "Test AI", "key": ""},
}

# job -> (plain-language name, kind). kind decides the default route.
JOBS = {
    "claims": ("Find claims in a draft", "text"),
    "tone": ("Review tone against the tone rules", "text"),
    "image": ("Read text in images and suggest alt text", "image"),
    "verify": ("Second opinion on serious AI findings", "verify"),
    "import": ("Draft rules from an uploaded guideline", "text"),
    "clarify": ("Suggest clearer wording for unclear rules", "text"),
}

DEFAULT_ROUTES = {
    # gemini-2.5-* returns HTTP 404 for new Google AI Studio projects since late 2026, so 3.5 is the default
    "text": "gemini:gemini-3.5-flash, gemini:gemini-3.5-flash-lite, gemini:gemini-flash-latest, groq:openai/gpt-oss-120b",
    "image": "gemini:gemini-3.5-flash, gemini:gemini-3.5-flash-lite, gemini:gemini-flash-latest, groq:qwen/qwen3.8-27b",
    # a different model family first, so the second opinion is independent of the first answer
    "verify": "groq:openai/gpt-oss-120b, gemini:gemini-3.5-flash-lite, gemini:gemini-flash-latest",
}

RATE_LIMITS = {"gemini": "AI_MAX_CALLS_PER_MINUTE", "groq": "GROQ_MAX_CALLS_PER_MINUTE"}
RATE_DEFAULTS = {"gemini": 12, "groq": 25, "openai": 30, "fake": 10_000}
COOLDOWN_SECONDS = 60


class AIError(Exception):
    def __init__(self, message: str, retry_next: bool = True):
        super().__init__(message)
        self.retry_next = retry_next


def parse_route(text: str) -> list[tuple[str, str]]:
    """'gemini:gemini-2.5-flash, groq:openai/gpt-oss-120b' -> [('gemini', 'gemini-2.5-flash'), ...]"""
    steps = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        provider, model = part.split(":", 1)
        provider, model = provider.strip().lower(), model.strip()
        if provider in PROVIDERS and model and (provider, model) not in steps:
            steps.append((provider, model))
    return steps


def route_text(steps: list[tuple[str, str]]) -> str:
    return ", ".join(f"{p}:{m}" for p, m in steps)


class AIGateway:
    def __init__(self, db: Database | None):
        self.db = db
        self.mode = os.environ.get("AI_PROVIDER", "").strip().lower()  # "", none, fake, gemini, groq, openai
        self.timeout = float(os.environ.get("AI_TIMEOUT_SECONDS", "25"))
        self._recent: dict[str, deque] = {}
        self._cool: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._fake = None
        self.use_cache = True

    # ------------------------------------------------------------------ routes
    def available(self, provider: str) -> bool:
        if self.mode == "none":
            return False
        if provider == "fake":
            return self.mode == "fake"
        if self.mode == "fake":
            return False
        key = PROVIDERS[provider]["key"]
        return bool(os.environ.get(key))

    def _saved_routes(self) -> dict:
        if not self.db:
            return {}
        try:
            return json.loads(self.db.get_meta("ai_routes") or "{}")
        except Exception:
            return {}

    def configured_route(self, job: str) -> tuple[list[tuple[str, str]], str]:
        """The route for a job before filtering by available keys, plus where it came from."""
        if self.mode == "fake":
            return [("fake", "fake")], "tests"
        kind = JOBS.get(job, ("", "text"))[1]
        env = os.environ.get(f"AI_ROUTE_{job.upper()}")
        if env:
            return parse_route(env), f"AI_ROUTE_{job.upper()} setting"
        saved = self._saved_routes().get(job)
        if saved:
            return parse_route(saved), "chosen on the How it works page"
        if kind != "image" and kind != "verify" and os.environ.get("AI_ROUTE"):
            return parse_route(os.environ["AI_ROUTE"]), "AI_ROUTE setting"
        steps = parse_route(DEFAULT_ROUTES[kind])
        primary = os.environ.get("GEMINI_MODEL", "").strip()
        if primary:  # older setting: replace the first Gemini model
            steps = [("gemini", primary)] + [s for s in steps if s != ("gemini", primary)]
        if self.mode in ("gemini", "groq", "openai"):
            first = [s for s in steps if s[0] == self.mode]
            if self.mode == "openai" and not first:
                first = [("openai", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))]
            steps = first + [s for s in steps if s not in first]
        return steps, "default"

    def route(self, job: str) -> list[tuple[str, str]]:
        steps, _ = self.configured_route(job)
        return [s for s in steps if self.available(s[0])]

    # ------------------------------------------------------------------ info
    @property
    def enabled(self) -> bool:
        return bool(self.route("claims"))

    def job_enabled(self, job: str) -> bool:
        return bool(self.route(job))

    @property
    def provider(self) -> str:
        r = self.route("claims")
        return r[0][0] if r else "none"

    @property
    def model(self) -> str:
        r = self.route("claims")
        return r[0][1] if r else ""

    @property
    def label(self) -> str:
        r = self.route("claims")
        if not r:
            return "AI off (code checks only)"
        if r[0][0] == "fake":
            return "Test AI (canned answers)"
        backups = len(r) - 1
        extra = f" (+{backups} backup{'s' if backups != 1 else ''})" if backups else ""
        return f"{PROVIDERS[r[0][0]]['label']} · {r[0][1]}{extra}"

    def status(self) -> list[dict]:
        """One row per job for the settings table."""
        rows = []
        for job, (name, kind) in JOBS.items():
            steps, source = self.configured_route(job)
            rows.append({
                "job": job, "name": name, "kind": kind, "source": source, "route": route_text(steps),
                "steps": [{"provider": p, "model": m, "available": self.available(p),
                           "cooling": self._cool.get((p, m), 0) > time.time()} for p, m in steps],
            })
        return rows

    # ----------------------------------------------------------------- calls
    def json(self, purpose: str, system: str, prompt: str, image: bytes | None = None,
             image_mime: str = "image/png", route: list[tuple[str, str]] | None = None) -> tuple[dict | None, dict]:
        """Ask for a JSON object. Returns (data or None, meta). Never raises."""
        steps = route if route is not None else self.route(purpose)
        meta = {"purpose": purpose, "provider": steps[0][0] if steps else "none", "model": steps[0][1] if steps else "",
                "ok": False, "cached": False, "error": "", "ms": 0, "attempts": [], "fallback": False}
        if not steps:
            meta["error"] = "AI is not configured"
            return None, meta
        key = hashlib.sha256("\x1f".join([
            purpose, route_text(steps), system, prompt, hashlib.sha256(image).hexdigest() if image else ""]).encode()
        ).hexdigest()
        if self.use_cache and self.db:
            try:
                cached = self.db.scalar("SELECT response FROM ai_cache WHERE key = ?", (key,))
            except Exception:
                cached = None
            if cached:
                stored = json.loads(cached)
                data = stored.get("data") if isinstance(stored, dict) and "data" in stored else stored
                if isinstance(stored, dict) and stored.get("provider"):
                    meta.update(provider=stored["provider"], model=stored.get("model", ""))
                meta.update(ok=True, cached=True)
                self._log(meta)
                return data, meta
        t0 = time.time()
        now = time.time()
        # steps that recently failed go to the back of the queue (but are still tried as a last resort)
        order = [s for s in steps if self._cool.get(s, 0) <= now] + [s for s in steps if self._cool.get(s, 0) > now]
        errors, dead = [], set()
        for i, (provider, model) in enumerate(order):
            if provider in dead:
                continue
            t1 = time.time()
            attempt = {"provider": provider, "model": model, "ok": False, "error": "", "ms": 0}
            try:
                if not self._allow(provider):
                    raise AIError("local rate limit reached (protecting the free quota)")
                if provider == "gemini":
                    text = self._gemini(model, system, prompt, image, image_mime)
                elif provider in ("groq", "openai"):
                    text = self._openai_compatible(provider, model, system, prompt, image, image_mime)
                else:
                    text = self._fake_answer(purpose, prompt, model)
                data = parse_json(text)
                if not isinstance(data, dict):
                    raise AIError("the model did not return a JSON object")
                attempt.update(ok=True, ms=int((time.time() - t1) * 1000))
                meta["attempts"].append(attempt)
                meta.update(ok=True, provider=provider, model=model, fallback=i > 0 or (provider, model) != steps[0])
                self._cool.pop((provider, model), None)
                if self.db:
                    try:
                        self.db.execute(
                            "INSERT INTO ai_cache (key, response, created_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING",
                            (key, json.dumps({"data": data, "provider": provider, "model": model}, ensure_ascii=False),
                             now_iso()))
                    except Exception:
                        log.exception("could not cache AI answer")
                meta["ms"] = int((time.time() - t0) * 1000)
                self._log(meta)
                return data, meta
            except Exception as exc:
                msg = _short_error(exc)
                attempt.update(error=msg, ms=int((time.time() - t1) * 1000))
                meta["attempts"].append(attempt)
                errors.append(f"{provider}:{model}: {msg[:140]}")
                log.warning("AI %s via %s:%s failed: %s", purpose, provider, model, msg)
                if isinstance(exc, requests.Timeout) or re.match(r"HTTP (404|408|429|5\d\d)", msg):
                    self._cool[(provider, model)] = time.time() + COOLDOWN_SECONDS
                if isinstance(exc, AIError) and not exc.retry_next:
                    dead.add(provider)  # bad or missing key: other models of this provider won't help
        meta["error"] = "; ".join(errors)[:600]
        meta["ms"] = int((time.time() - t0) * 1000)
        self._log(meta)
        return None, meta

    def _allow(self, provider: str) -> bool:
        raw = os.environ.get(RATE_LIMITS.get(provider, "-"), "") if provider in RATE_LIMITS else ""
        limit = int(raw) if raw.strip().isdigit() else RATE_DEFAULTS.get(provider, 30)
        with self._lock:
            q = self._recent.setdefault(provider, deque())
            now = time.time()
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def _log(self, meta: dict) -> None:
        if not self.db:
            return
        try:
            self.db.execute(
                "INSERT INTO ai_calls (purpose, provider, model, ok, cached, ms, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (meta["purpose"], meta["provider"], meta["model"], int(meta["ok"]), int(meta["cached"]), meta["ms"],
                 (meta["error"] or "")[:300], now_iso()),
            )
        except Exception:
            log.exception("could not log AI call")

    # -------------------------------------------------------------- providers
    def _gemini(self, model: str, system: str, prompt: str, image: bytes | None, mime: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        parts = [{"text": prompt}]
        if image:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}})
        config = {"temperature": 0, "responseMimeType": "application/json"}
        if model.startswith("gemini-2.5-flash"):
            # these checks need speed, not long reasoning; newer models reject thinkingBudget 0 (HTTP 400)
            config["thinkingConfig"] = {"thinkingBudget": 0}
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": config,
        }
        r = requests.post(url, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                          json=body, timeout=self.timeout)
        if r.status_code >= 400:
            raise AIError(f"HTTP {r.status_code}: {r.text[:200]}", retry_next=r.status_code not in (401, 403))
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise AIError(f"Gemini returned no answer ({reason})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise AIError(f"Gemini returned an empty answer ({candidates[0].get('finishReason', '')})")
        return text

    def _openai_compatible(self, provider: str, model: str, system: str, prompt: str, image: bytes | None,
                           mime: str) -> str:
        info = PROVIDERS[provider]
        base = (os.environ.get(info.get("base_env", ""), "") or info["base"]).rstrip("/")
        content: list[dict] | str
        if image:
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}}]
        else:
            content = prompt
        body = {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        if provider == "groq":
            body["max_completion_tokens"] = 4096
            if "gpt-oss" in model:
                body["reasoning_effort"] = "low"
            elif "qwen" in model:
                body["reasoning_format"] = "hidden"
        r = requests.post(f"{base}/chat/completions", json=body, timeout=self.timeout,
                          headers={"Authorization": f"Bearer {os.environ[info['key']]}", "Content-Type": "application/json"})
        if r.status_code >= 400:
            raise AIError(f"HTTP {r.status_code}: {r.text[:200]}", retry_next=r.status_code not in (401, 403))
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise AIError("unexpected response shape")

    def _fake_answer(self, purpose: str, prompt: str, model: str = "fake") -> str:
        if model == "busy":  # lets the tests simulate an overloaded model
            raise AIError("HTTP 503: simulated busy model")
        if self._fake is None:
            path = os.environ.get("FAKE_AI_FILE", "tests/fake_ai.json")
            with open(path, encoding="utf-8") as fh:
                self._fake = json.load(fh)
        answers = self._fake.get(purpose, [])
        for item in answers:  # first answer whose trigger text appears in the prompt
            if item.get("when", "") in prompt:
                if item.get("fail"):
                    raise AIError(f"HTTP {item['fail']}: simulated failure")
                return json.dumps(item["answer"])
        return json.dumps({})


def parse_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _short_error(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "could not connect"
    msg = str(exc) or type(exc).__name__
    msg = re.sub(r"key=[^&\s]+", "key=***", msg)
    msg = re.sub(r"(gsk_|sk-|AIza)[A-Za-z0-9_\-]{8,}", r"\1***", msg)
    return msg[:240]
