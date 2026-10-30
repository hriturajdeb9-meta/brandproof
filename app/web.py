"""Shared web plumbing: app state, the signed-in context of each request, page rendering and form helpers."""
from __future__ import annotations

import io
import json
import logging
import os
import re
from datetime import datetime
from http.cookies import SimpleCookie
from urllib.parse import quote

from markupsafe import Markup
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from . import auth
from .ai import AIGateway
from .db import Database
from .kit import DIMENSION_LABELS, DIMENSIONS

log = logging.getLogger("brandproof")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_TEXT = 12000
MAX_UPLOAD = 8 * 1024 * 1024
APP_VERSION = "3.2"


class State:
    db: Database | None = None
    ai: AIGateway | None = None
    ai_off: AIGateway | None = None
    sessions: auth.Sessions | None = None
    jobs = None
    demo_ws: int | None = None
    throttle = auth.Throttle()


S = State()
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

STATUS = {
    "checked": ("Checked", "Fix the findings, then submit"),
    "in_review": ("In review", "Waiting for a reviewer"),
    "changes_requested": ("Changes requested", "The reviewer asked for changes"),
    "approved": ("Approved", "Version locked with an approval record"),
}
CHANNELS = ["Instagram", "LinkedIn", "X / Twitter", "Email", "Blog", "Website", "Ad", "Brochure", "Other"]
EDITABLE = {"checked", "changes_requested"}

try:
    from zoneinfo import ZoneInfo
    DISPLAY_TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Asia/Kolkata"))
except Exception:  # no tz database available
    from datetime import timedelta, timezone as _tz
    DISPLAY_TZ = _tz(timedelta(hours=5, minutes=30))

ACTIVITY = {
    "draft.created": "created the draft",
    "check.completed": "ran the checks",
    "check.failed": "ran the checks, which failed",
    "finding.accept": "accepted a fix",
    "finding.edit": "edited a fix",
    "finding.reject": "rejected a finding",
    "finding.acknowledge": "will fix it in the design",
    "finding.accept_all": "accepted all suggested fixes",
    "finding.superseded": "resolved a finding with another fix",
    "version.created": "saved a new version",
    "working.discarded": "discarded working changes",
    "draft.submitted": "submitted for review",
    "draft.auto_approved": "submitted; approved automatically by policy",
    "review.approved": "approved",
    "review.changes_requested": "requested changes",
    "comment.added": "commented",
    "kit.seeded": "loaded a Brand Kit",
    "kit.restored": "restored the demo Brand Kit",
    "team.updated": "renamed the demo people",
    "team.reset": "reset the demo people to their default names",
    "member.invited": "added a team member",
    "member.updated": "changed a team member's role",
    "member.removed": "removed a team member",
    "workspace.created": "created the workspace",
    "guideline.uploaded": "uploaded a guideline",
    "guideline.read": "finished reading a guideline",
    "candidate.approved": "approved a drafted rule",
    "candidate.discarded": "discarded a drafted rule",
    "rule.tested": "ran a rule's example tests",
    "rule.reworded": "approved clearer wording for a rule",
    "record.downloaded": "downloaded the approval record",
    "expiry.rechecked": "re-checked approved content with expiring facts",
}


def when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(DISPLAY_TZ).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso


def activity(action: str) -> str:
    if action in ACTIVITY:
        return ACTIVITY[action]
    if action.startswith("kit."):
        parts = action.split(".")
        return f"{parts[-1]} a Brand Kit {parts[1].rstrip('s')} entry" if len(parts) == 3 else action
    return action.replace(".", " ")


templates.env.filters["when"] = when
templates.env.filters["activity"] = activity
templates.env.filters["fromjson"] = lambda s: json.loads(s or "{}")


# --------------------------------------------------------------------------- request context
class Ctx:
    """Who is making this request, and in which workspace."""

    def __init__(self, user: dict, member: dict | None, all_members: list[dict]):
        self.user = user
        self.member = member
        self.memberships = all_members
        self.ws = member["workspace_id"] if member else None
        self.ws_name = member["name"] if member else ""
        self.is_demo = bool(member and member.get("is_demo"))
        self.role = member["role"] if member else None
        self.title = (member.get("title") if member else "") or (auth.ROLES[self.role]["label"] if self.role else "")
        self.name = user["name"]
        self.uid = user["id"]
        self.new_cookie: str | None = None

    @property
    def who(self) -> tuple[str, str]:
        return self.name, self.role or ""


def ctx_of(request: Request) -> Ctx:
    c = request.scope.get("bp_ctx")
    if c is None:
        raise HTTPException(401, "Please sign in.")
    return c


def need_ws(request: Request) -> Ctx:
    c = ctx_of(request)
    if not c.ws:
        raise HTTPException(409, "Create or join a workspace first.")
    return c


_CACHE: dict = {}
_CACHE_SECONDS = 20


def cached(key, fn):
    """Tiny time-based cache for lookups done on every page (the database may be far from the server).
    Safe for roles: PostgreSQL row-level security still checks the live membership on every query."""
    import time
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    val = fn()
    if len(_CACHE) > 2000:
        _CACHE.clear()
    _CACHE[key] = (time.time(), val)
    return val


def forget(*prefixes) -> None:
    for k in list(_CACHE):
        if not prefixes or (isinstance(k, tuple) and k[0] in prefixes):
            _CACHE.pop(k, None)


def people(ws: int | None) -> dict:
    return cached(("people", ws), lambda: _people(ws))


def _people(ws: int | None) -> dict:
    """Role -> first member with that role (name and title); used for hints like 'Switch to Meera'."""
    out = {k: {"name": "", "label": v["label"], "about": v["can"]} for k, v in auth.ROLES.items()}
    if not ws:
        return out
    with S.db.as_system():
        rows = S.db.all("SELECT m.role, m.title, u.name FROM members m JOIN users u ON u.id = m.user_id "
                        "WHERE m.workspace_id = ? ORDER BY m.id", (ws,))
    for r in rows:
        if r["role"] in out and not out[r["role"]]["name"]:
            out[r["role"]]["name"] = r["name"]
            out[r["role"]]["label"] = r["title"] or out[r["role"]]["label"]
    return out


def need_role(request: Request, *allowed: str) -> Ctx:
    c = need_ws(request)
    if c.role not in allowed:
        roles = people(c.ws)
        if c.is_demo:
            names = " or ".join(f"{roles[a]['name']} ({roles[a]['label']})" for a in allowed if roles[a]["name"])
            raise HTTPException(403, f"Switch to {names} (top right) to do this.")
        labels = " or ".join(auth.ROLES[a]["label"].lower() for a in allowed)
        raise HTTPException(403, f"Only a {labels} in this workspace can do this. Ask your brand manager to change your role.")
    return c


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    c: Ctx | None = request.scope.get("bp_ctx")
    roles = people(c.ws if c else None)
    review_count = 0
    if c and c.ws:
        review_count = S.db.scalar("SELECT COUNT(*) AS c FROM drafts WHERE workspace_id = ? AND status = 'in_review'",
                                   (c.ws,)) or 0
    base = {
        "c": c, "role": c.role if c else None, "roles": roles,
        "persona": {"name": c.name if c else "", "label": c.title if c else ""},
        "is_demo": bool(c and c.is_demo), "demo_people": auth.DEMO_PEOPLE, "demo_password": auth.DEMO_PASSWORD,
        "ai_label": S.ai.label, "ai_on": S.ai.enabled, "storage": S.db.kind, "rls": S.db.rls,
        "storage_warning": S.db.warning, "review_count": review_count, "app_version": APP_VERSION,
        "STATUS": STATUS, "DIMENSION_LABELS": DIMENSION_LABELS, "DIMENSIONS": DIMENSIONS, "ROLES": auth.ROLES,
        "path": request.url.path, "flash": request.query_params.get("msg", "")[:400],
        "flash_kind": request.query_params.get("kind", "ok") if request.query_params.get("kind") in ("ok", "error") else "ok",
        "supabase": auth.supabase_on(),
    }
    base.update(ctx or {})
    resp = templates.TemplateResponse(request, name, base, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def go(url: str, msg: str = "", kind: str = "ok") -> RedirectResponse:
    if not url.startswith("/") or url.startswith("//"):
        url = "/"
    if msg:
        frag = ""
        if "#" in url:
            url, frag = url.split("#", 1)
            frag = "#" + frag
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={quote(msg[:400])}&kind={kind}{frag}"
    return RedirectResponse(url, status_code=303)


def safe_back(url: str | None, default: str = "/") -> str:
    url = (url or "").strip()
    return url if url.startswith("/") and not url.startswith("//") else default


async def read_form(request: Request):
    try:
        return await request.form(max_files=4, max_fields=200, max_part_size=MAX_UPLOAD)
    except Exception as exc:
        raise HTTPException(400, f"Could not read the form ({type(exc).__name__}). If you attached a file, keep it under 8 MB.")


async def read_upload(upload, limit: int = MAX_UPLOAD) -> bytes | None:
    if upload is None or isinstance(upload, str) or not getattr(upload, "filename", ""):
        return None
    data = await upload.read()
    if len(data) > limit:
        raise HTTPException(413, f"That file is too large ({len(data) // 1024 // 1024} MB). The limit is {limit // 1024 // 1024} MB.")
    return data or None


def docx_text(data: bytes) -> str:
    import docx  # python-docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "That file doesn't look like a valid Word (.docx) document.")
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(p for p in parts).strip()


def docx_sections(data: bytes) -> list[str]:
    """Split a Word document into 'pages' at headings (Word files have no fixed pages)."""
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "That file doesn't look like a valid Word (.docx) document.")
    sections, cur = [], []
    for p in document.paragraphs:
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading") and cur and sum(len(x) for x in cur) > 200:
            sections.append("\n".join(cur).strip())
            cur = []
        if p.text.strip():
            cur.append(p.text)
    for table in document.tables:
        for row in table.rows:
            cur.append(" | ".join(c.text for c in row.cells))
    if cur:
        sections.append("\n".join(cur).strip())
    return [s for s in sections if s]


def pdf_pages(data: bytes, max_pages: int = 60) -> list[str]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise HTTPException(400, "That PDF is password-protected. Remove the password and upload it again.")
        pages = []
        for page in reader.pages[:max_pages]:
            try:
                text = (page.extract_text() or "").replace("\x7f", "•")
                text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
                pages.append(re.sub(r"[ \t]+\n", "\n", text).strip())
            except Exception:
                pages.append("")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "That file doesn't look like a valid PDF.")
    if not any(p.strip() for p in pages):
        raise HTTPException(400, "No text could be read from this PDF. It may be a scanned image; "
                                 "export it with selectable text, or upload a Word file.")
    return pages


def parse_locked(raw: str) -> list[str]:
    items = [x.strip() for x in re.split(r"[\n,]", raw or "") if x.strip()]
    return items[:10]


# --------------------------------------------------------------------------- session middleware
def _cookie(scope, name: str) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            try:
                jar = SimpleCookie(v.decode("latin-1"))
            except Exception:
                return None
            if name in jar:
                return jar[name].value
    return None


def _is_https(scope) -> bool:
    for k, v in scope.get("headers") or []:
        if k == b"x-forwarded-proto":
            return v.decode().split(",")[0].strip() == "https"
    return scope.get("scheme") == "https"


def session_cookie_header(value: str, https: bool, clear: bool = False) -> bytes:
    parts = [f"{auth.COOKIE}={'' if clear else value}", "Path=/", "HttpOnly", "SameSite=Lax",
             f"Max-Age={0 if clear else auth.SESSION_DAYS * 86400}"]
    if https:
        parts.append("Secure")
    return "; ".join(parts).encode()


def build_ctx(user_id: int, workspace_id: int | None) -> Ctx | None:
    return cached(("ctx", user_id, workspace_id), lambda: _build_ctx(user_id, workspace_id))


def _build_ctx(user_id: int, workspace_id: int | None) -> Ctx | None:
    user = auth.get_user(S.db, user_id)
    if not user:
        return None
    all_m = auth.memberships(S.db, user_id)
    member = auth.membership(S.db, user_id, workspace_id) if workspace_id else None
    if not member and all_m:
        pick = next((m for m in all_m if m["workspace_id"] == user.get("last_workspace")), all_m[0])
        member = auth.membership(S.db, user_id, pick["workspace_id"])
    return Ctx(user, member, all_m)


class SessionMiddleware:
    """Works out who is signed in. Visitors without a session enter the public demo as the creator,
    so the project link works with no sign-in at all."""

    SKIP = ("/static/", "/healthz")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(self.SKIP):
            return await self.app(scope, receive, send)
        payload = S.sessions.read(_cookie(scope, auth.COOKIE))
        c = build_ctx(payload["u"], payload.get("w")) if payload else None
        fresh = None
        if c is None and S.demo_ws:
            uid = auth.demo_user_id(S.db, "creator")
            c = build_ctx(uid, S.demo_ws) if uid else None
            if c:
                fresh = S.sessions.sign(c.uid, c.ws)
        scope["bp_ctx"] = c
        token = S.db.set_acting(c.uid, c.ws) if c and c.ws else S.db.set_acting(None, None)
        https = _is_https(scope)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                new = scope.get("bp_new_cookie")
                headers = list(message.get("headers") or [])
                if new is not None:
                    headers.append((b"set-cookie", session_cookie_header(new, https, clear=(new == ""))))
                elif fresh:
                    headers.append((b"set-cookie", session_cookie_header(fresh, https)))
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"same-origin"))
                headers.append((b"x-frame-options", b"SAMEORIGIN"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                from .db import _acting
                _acting.reset(token)
            except Exception:
                pass


def set_session(request: Request, user_id: int, workspace_id: int | None) -> None:
    """Ask the middleware to send a new session cookie with this response."""
    request.scope["bp_new_cookie"] = S.sessions.sign(user_id, workspace_id)
    forget()
    if workspace_id:
        user = auth.get_user(S.db, user_id)
        if not user or user.get("last_workspace") != workspace_id:  # skip the write (and its round trip) if unchanged
            with S.db.as_system():
                S.db.execute("UPDATE users SET last_workspace = ? WHERE id = ?", (workspace_id, user_id))


def clear_session(request: Request) -> None:
    request.scope["bp_new_cookie"] = ""


def markup_json(obj) -> Markup:
    return Markup(json.dumps(obj, ensure_ascii=False).replace("</", "<\\/"))
