"""LLM gateway: the only module that talks to an AI provider.

Providers (chosen with environment variables):
  * gemini  - Google Gemini API (default when GEMINI_API_KEY is set)
  * openai  - any OpenAI-compatible chat API (OpenAI, Groq, ...) via OPENAI_API_KEY / OPENAI_BASE_URL
  * fake    - canned answers from a JSON file, used by the automated tests
  * none    - AI switched off; Brandproof runs its code checks only

Every call is logged to the `ai_calls` table and answers are cached by a hash of
(provider, model, prompt, image), so the same draft gives the same report.
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


class AIError(Exception):
    pass


class AIGateway:
    def __init__(self, db: Database):
        self.db = db
        provider = os.environ.get("AI_PROVIDER", "").strip().lower()
        if not provider:
            if os.environ.get("GEMINI_API_KEY"):
                provider = "gemini"
            elif os.environ.get("OPENAI_API_KEY"):
                provider = "openai"
            else:
                provider = "none"
        self.provider = provider
        if provider == "gemini":
            self.model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            # tried in order when a model is busy (HTTP 429/5xx), missing (404) or too slow
            fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-flash-latest")
            self.models = [self.model] + [m.strip() for m in fallbacks.split(",") if m.strip() and m.strip() != self.model]
        elif provider == "openai":
            self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        elif provider == "fake":
            self.model = "fake"
        else:
            self.model = ""
        self.timeout = float(os.environ.get("AI_TIMEOUT_SECONDS", "25"))
        self.max_per_minute = int(os.environ.get("AI_MAX_CALLS_PER_MINUTE", "12"))
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()
        self._fake = None

    # ------------------------------------------------------------------ info
    @property
    def enabled(self) -> bool:
        if self.provider == "gemini":
            return bool(os.environ.get("GEMINI_API_KEY"))
        if self.provider == "openai":
            return bool(os.environ.get("OPENAI_API_KEY"))
        return self.provider == "fake"

    @property
    def label(self) -> str:
        if not self.enabled:
            return "AI off (code checks only)"
        names = {"gemini": "Google Gemini", "openai": "OpenAI-compatible API", "fake": "Test AI (canned answers)"}
        return f"{names.get(self.provider, self.provider)} · {self.model}"

    # ----------------------------------------------------------------- calls
    def json(self, purpose: str, system: str, prompt: str, image: bytes | None = None,
             image_mime: str = "image/png") -> tuple[dict | None, dict]:
        """Ask the model for a JSON object. Returns (data or None, meta)."""
        meta = {"purpose": purpose, "provider": self.provider, "model": self.model, "ok": False, "cached": False,
                "error": "", "ms": 0}
        if not self.enabled:
            meta["error"] = "AI is not configured"
            return None, meta
        key = hashlib.sha256(
            "\x1f".join([self.provider, self.model, purpose, system, prompt,
                         hashlib.sha256(image).hexdigest() if image else ""]).encode()
        ).hexdigest()
        cached = self.db.scalar("SELECT response FROM ai_cache WHERE key = ?", (key,))
        if cached:
            meta.update(ok=True, cached=True)
            self._log(meta)
            return json.loads(cached), meta
        if not self._allow():
            meta["error"] = "Rate limit reached (protecting the free AI quota); try again in a minute"
            self._log(meta)
            return None, meta
        t0 = time.time()
        try:
            if self.provider == "gemini":
                text = self._gemini(system, prompt, image, image_mime, meta)
            elif self.provider == "openai":
                text = self._openai(system, prompt, image, image_mime)
            else:
                text = self._fake_answer(purpose, prompt)
            data = parse_json(text)
            if not isinstance(data, dict):
                raise AIError("Model did not return a JSON object")
            meta["ok"] = True
            self.db.execute(
                "INSERT INTO ai_cache (key, response, created_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING",
                (key, json.dumps(data, ensure_ascii=False), now_iso()),
            )
            return data, meta
        except Exception as exc:  # the checks continue without AI
            meta["error"] = _short_error(exc)
            log.warning("AI call %s failed: %s", purpose, meta["error"])
            return None, meta
        finally:
            meta["ms"] = int((time.time() - t0) * 1000)
            if not meta["cached"]:
                self._log(meta)

    def _allow(self) -> bool:
        with self._lock:
            now = time.time()
            while self._recent and now - self._recent[0] > 60:
                self._recent.popleft()
            if len(self._recent) >= self.max_per_minute:
                return False
            self._recent.append(now)
            return True

    def _log(self, meta: dict) -> None:
        try:
            self.db.execute(
                "INSERT INTO ai_calls (purpose, provider, model, ok, cached, ms, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (meta["purpose"], meta["provider"], meta["model"], int(meta["ok"]), int(meta["cached"]), meta["ms"],
                 meta["error"][:300], now_iso()),
            )
        except Exception:
            log.exception("could not log AI call")

    # -------------------------------------------------------------- providers
    def _post(self, url: str, headers: dict, body: dict) -> dict:
        last = None
        for attempt in range(2):
            r = requests.post(url, headers=headers, json=body, timeout=self.timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                last = r
                time.sleep(2.0)
                continue
            if r.status_code >= 400:
                raise AIError(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        raise AIError(f"HTTP {last.status_code if last is not None else '?'} after retry")

    def _gemini(self, system: str, prompt: str, image: bytes | None, mime: str, meta: dict | None = None) -> str:
        """Try each configured Gemini model in turn; a busy, missing or slow model falls through to the next."""
        errors = []
        for model in self.models:
            try:
                text = self._gemini_once(model, system, prompt, image, mime)
                if meta is not None:
                    meta["model"] = model
                return text
            except requests.Timeout:
                errors.append(f"{model}: timed out")
            except AIError as exc:
                msg = str(exc)
                errors.append(f"{model}: {msg[:120]}")
                if not re.match(r"HTTP (404|429|5\d\d)", msg):
                    break  # bad key or bad request: another model won't help
        raise AIError("; ".join(errors) or "no Gemini model configured")

    def _gemini_once(self, model: str, system: str, prompt: str, image: bytes | None, mime: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        parts = [{"text": prompt}]
        if image:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}})
        config = {"temperature": 0, "responseMimeType": "application/json"}
        if model.startswith("gemini-2.5-flash"):
            config["thinkingConfig"] = {"thinkingBudget": 0}  # these checks need speed, not long reasoning
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": config,
        }
        r = requests.post(url, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
                          json=body, timeout=self.timeout)
        if r.status_code >= 400:
            raise AIError(f"HTTP {r.status_code}: {r.text[:200]}")
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

    def _openai(self, system: str, prompt: str, image: bytes | None, mime: str) -> str:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}})
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        data = self._post(f"{base}/chat/completions",
                          {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
                          body)
        return data["choices"][0]["message"]["content"]

    def _fake_answer(self, purpose: str, prompt: str) -> str:
        if self._fake is None:
            path = os.environ.get("FAKE_AI_FILE", "tests/fake_ai.json")
            with open(path, encoding="utf-8") as fh:
                self._fake = json.load(fh)
        answers = self._fake.get(purpose, [])
        for item in answers:  # first answer whose trigger text appears in the prompt
            if item.get("when", "") in prompt:
                return json.dumps(item["answer"])
        return json.dumps({})


def parse_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _short_error(exc: Exception) -> str:
    msg = str(exc) or type(exc).__name__
    msg = re.sub(r"key=[^&\s]+", "key=***", msg)
    return msg[:240]
