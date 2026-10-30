"""Sign-in, sessions and workspaces.

Two ways to sign in, chosen automatically:
  * Supabase Auth (email + password), when SUPABASE_URL and SUPABASE_ANON_KEY are set. Supabase stores and
    checks the password; Brandproof only keeps the user's email, name and Supabase user id.
  * Built-in accounts otherwise (local development, or a clone without Supabase). Passwords are hashed with
    scrypt and a random salt.
Demo people (Priya, Meera, Arjun) are real user rows in the public demo workspace that anyone can enter in
one click, without a password. Their permissions are enforced exactly like everyone else's.

The session is a signed cookie (HMAC-SHA256) holding the user id, the active workspace and an expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque

import requests

from .db import Database, now_iso

log = logging.getLogger("brandproof.auth")

COOKIE = "bp_session"
SESSION_DAYS = 14
ROLES = {
    "creator": {"label": "Creator", "can": "Writes drafts, fixes findings, submits for review"},
    "reviewer": {"label": "Reviewer", "can": "Approves drafts or requests changes"},
    "brand": {"label": "Brand manager", "can": "Owns the Brand Kit and the team; can also write and review"},
}
DEMO_PEOPLE = {
    "creator": {"email": "priya@demo.brandproof", "name": "Priya", "title": "Creator"},
    "reviewer": {"email": "meera@demo.brandproof", "name": "Meera", "title": "Reviewer · Compliance"},
    "brand": {"email": "arjun@demo.brandproof", "name": "Arjun", "title": "Brand manager"},
}
DEMO_PASSWORD = "demo2026"  # shown on the sign-in page: the demo people are public on purpose
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


class AuthError(Exception):
    pass


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except Exception:
        return False


def check_new_password(password: str) -> None:
    if len(password or "") < 8:
        raise AuthError("Use a password of at least 8 characters.")
    if len(password) > 200:
        raise AuthError("That password is too long.")


def clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    if email.endswith("@demo.brandproof"):
        raise AuthError("That address is reserved for the demo people.")
    return email


# --------------------------------------------------------------------------- sessions
class Sessions:
    def __init__(self, db: Database):
        secret = os.environ.get("SESSION_SECRET", "").strip()
        if not secret:  # keep sessions valid across restarts without any setup
            secret = db.get_meta("session_secret")
            if not secret:
                db.claim_once("session_secret_lock")
                secret = db.get_meta("session_secret") or secrets.token_hex(32)
                db.set_meta("session_secret", secret)
        self.key = hashlib.sha256(("brandproof-session:" + secret).encode()).digest()

    def sign(self, user_id: int, workspace_id: int | None) -> str:
        payload = {"u": int(user_id), "w": int(workspace_id) if workspace_id else None,
                   "x": int(time.time()) + SESSION_DAYS * 86400}
        raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(hmac.new(self.key, raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return f"{raw}.{sig}"

    def read(self, cookie: str | None) -> dict | None:
        if not cookie or "." not in cookie:
            return None
        raw, sig = cookie.rsplit(".", 1)
        good = base64.urlsafe_b64encode(hmac.new(self.key, raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(sig, good):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except ValueError:
            return None
        if payload.get("x", 0) < time.time():
            return None
        return payload


# --------------------------------------------------------------------------- throttling
class Throttle:
    """At most `limit` attempts per key in `window` seconds (protects sign-in from password guessing)."""

    def __init__(self, limit: int = 8, window: int = 300):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self.lock:
            q = self.hits.setdefault(key, deque())
            now = time.time()
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


# --------------------------------------------------------------------------- Supabase Auth
def supabase_on() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_ANON_KEY"))


def _sb(path: str, body: dict, token: str | None = None, method: str = "POST") -> tuple[int, dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/auth/v1/" + path
    headers = {"apikey": os.environ["SUPABASE_ANON_KEY"], "Content-Type": "application/json"}
    headers["Authorization"] = f"Bearer {token or os.environ['SUPABASE_ANON_KEY']}"
    try:
        r = requests.request(method, url, json=body, headers=headers, timeout=15)
    except requests.RequestException as exc:
        raise AuthError(f"The sign-in service could not be reached ({type(exc).__name__}). Try again in a moment.")
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data


def _sb_message(data: dict, fallback: str) -> str:
    msg = str(data.get("msg") or data.get("error_description") or data.get("message") or data.get("error") or "")
    low = msg.lower()
    if "invalid login" in low or "invalid credentials" in low:
        return "Wrong email or password."
    if "not confirmed" in low:
        return "Please confirm your email first (check your inbox), then sign in."
    if "already registered" in low or "already exists" in low:
        return "An account with this email already exists. Sign in instead."
    if "rate limit" in low or "too many" in low:
        return "Too many attempts. Please wait a minute and try again."
    if "password" in low and ("weak" in low or "characters" in low or "should" in low):
        return msg
    return msg or fallback


def supabase_sign_up(email: str, password: str, name: str, redirect: str) -> dict:
    status, data = _sb(f"signup?redirect_to={requests.utils.quote(redirect, safe='')}",
                       {"email": email, "password": password, "data": {"name": name}})
    if status >= 400:
        raise AuthError(_sb_message(data, "Could not create the account."))
    user = data.get("user") or (data if data.get("id") else {})
    return {"auth_id": user.get("id"), "confirmed": bool(data.get("access_token"))}


def supabase_sign_in(email: str, password: str) -> str:
    status, data = _sb("token?grant_type=password", {"email": email, "password": password})
    if status >= 400:
        raise AuthError(_sb_message(data, "Wrong email or password."))
    return (data.get("user") or {}).get("id") or ""


def supabase_send_reset(email: str, redirect: str) -> None:
    status, data = _sb(f"recover?redirect_to={requests.utils.quote(redirect, safe='')}", {"email": email})
    if status >= 400 and status != 404:
        raise AuthError(_sb_message(data, "Could not send the reset email."))


def supabase_set_password(access_token: str, password: str) -> str:
    status, data = _sb("user", {"password": password}, token=access_token, method="PUT")
    if status >= 400:
        raise AuthError(_sb_message(data, "The reset link has expired. Ask for a new one."))
    return str(data.get("email") or "").lower()


# --------------------------------------------------------------------------- users and workspaces
def find_user(db: Database, email: str) -> dict | None:
    with db.as_system():
        return db.one("SELECT * FROM users WHERE email = ?", (email.lower(),))


def get_user(db: Database, user_id: int) -> dict | None:
    with db.as_system():
        return db.one("SELECT id, email, name, auth, last_workspace FROM users WHERE id = ?", (user_id,))


def create_user(db: Database, email: str, name: str, auth: str, password: str | None = None,
                auth_id: str | None = None) -> int:
    with db.as_system():
        return db.insert(
            "INSERT INTO users (email, name, auth, auth_id, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (email.lower(), name, auth, auth_id, hash_password(password) if password else None, now_iso()))


def attach_invites(db: Database, user: dict) -> None:
    """Link membership invitations made for this email address to the user."""
    with db.as_system():
        db.execute("UPDATE members SET user_id = ? WHERE email = ? AND user_id IS NULL", (user["id"], user["email"]))


def memberships(db: Database, user_id: int) -> list[dict]:
    with db.as_system():
        return db.all(
            "SELECT m.workspace_id, m.role, m.title, w.name, w.is_demo FROM members m JOIN workspaces w ON w.id = m.workspace_id "
            "WHERE m.user_id = ? ORDER BY w.is_demo, w.name", (user_id,))


def membership(db: Database, user_id: int, workspace_id: int) -> dict | None:
    with db.as_system():
        return db.one(
            "SELECT m.workspace_id, m.role, m.title, w.name, w.is_demo, w.disclaimer FROM members m "
            "JOIN workspaces w ON w.id = m.workspace_id WHERE m.user_id = ? AND m.workspace_id = ?",
            (user_id, workspace_id))


def create_workspace(db: Database, user: dict, name: str, title: str = "Brand manager") -> int:
    name = re.sub(r"\s+", " ", name or "").strip()[:60]
    if len(name) < 2:
        raise AuthError("Give the workspace a name (for example your brand or club).")
    with db.as_system():
        ws = db.insert("INSERT INTO workspaces (name, is_demo, disclaimer, created_by, created_at) VALUES (?, 0, '', ?, ?) "
                       "RETURNING id", (name, user["id"], now_iso()))
        db.execute("INSERT INTO members (workspace_id, user_id, email, role, title, added_by, created_at) "
                   "VALUES (?, ?, ?, 'brand', ?, ?, ?)", (ws, user["id"], user["email"], title, user["name"], now_iso()))
        db.execute("UPDATE users SET last_workspace = ? WHERE id = ?", (ws, user["id"]))
    return ws


def demo_workspace_id(db: Database) -> int | None:
    with db.as_system():
        return db.scalar("SELECT id FROM workspaces WHERE is_demo = 1 ORDER BY id LIMIT 1")


def demo_user_id(db: Database, role: str) -> int | None:
    person = DEMO_PEOPLE.get(role)
    if not person:
        return None
    with db.as_system():
        return db.scalar("SELECT id FROM users WHERE email = ?", (person["email"],))


def ensure_demo_people(db: Database, workspace_id: int) -> None:
    with db.as_system():
        for role, p in DEMO_PEOPLE.items():
            uid = db.scalar("SELECT id FROM users WHERE email = ?", (p["email"],))
            if not uid:
                uid = db.insert("INSERT INTO users (email, name, auth, created_at) VALUES (?, ?, 'demo', ?) "
                                "ON CONFLICT (email) DO NOTHING RETURNING id", (p["email"], p["name"], now_iso()))
                uid = uid or db.scalar("SELECT id FROM users WHERE email = ?", (p["email"],))
            db.execute("INSERT INTO members (workspace_id, user_id, email, role, title, added_by, created_at) "
                       "VALUES (?, ?, ?, ?, ?, 'System', ?) ON CONFLICT (workspace_id, email) DO NOTHING",
                       (workspace_id, uid, p["email"], role, p["title"], now_iso()))
