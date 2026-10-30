"""Brandproof web app (Starlette + Jinja2, server-rendered). This module wires everything together."""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import auth, guidelines, insights, routes_account as acc, routes_drafts as dr, routes_kit as kit
from .ai import AIGateway
from .db import Database
from .jobs import Jobs
from .seed import seed_demo
from .web import HERE, S, SessionMiddleware, need_ws, render

def _load_dotenv(path: str = ".env") -> None:
    """Read KEY=value lines from a local .env file (never overrides real environment variables)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split(" #", 1)[0].strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("brandproof")


def init(database: Database | None = None) -> None:
    S.db = database or Database()
    S.db.init_schema()
    S.ai = AIGateway(S.db)
    S.ai_off = AIGateway(S.db)
    S.ai_off.mode = "none"
    S.sessions = auth.Sessions(S.db)
    S.jobs = S.jobs or Jobs(int(os.environ.get("BP_WORKERS", "3")))
    S.demo_ws = seed_demo(S.db)
    n = dr.recover_checks() + guidelines.recover_guidelines()
    log.info("Brandproof ready: storage=%s, rls=%s, AI=%s, auth=%s, resumed jobs=%s", S.db.kind, S.db.rls, S.ai.label,
             "supabase" if auth.supabase_on() else "built-in", n)


@asynccontextmanager
async def lifespan(app):
    if S.db is None:
        init()
    if os.environ.get("PORT") and S.db.kind == "postgres":  # hosted: warm the read cache for the demo people
        import threading
        threading.Thread(target=_warm_up, args=(int(os.environ["PORT"]),), daemon=True).start()
    yield


def _warm_up(port: int) -> None:
    """After a start (deploy or wake from sleep), open the main pages once as each demo person, so the first
    visitor's clicks are answered from memory instead of waiting on the distant database. Reads only."""
    import http.cookiejar
    import time
    import urllib.parse
    import urllib.request

    base = f"http://127.0.0.1:{port}"
    time.sleep(2)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        pages = {"reviewer": ["/review", "/kit", "/insights", "/"], "brand": ["/kit", "/insights", "/guidelines", "/team", "/"],
                 "creator": ["/kit", "/insights", "/review", "/team", "/about", "/drafts/new", "/"]}
        for role, urls in pages.items():
            body = urllib.parse.urlencode({"role": role, "back": "/"}).encode()
            opener.open(urllib.request.Request(base + "/demo/as", data=body, headers={"Origin": base}), timeout=30).read()
            for u in urls:
                opener.open(base + u, timeout=30).read()
        log.info("Read cache warmed for the demo")
    except Exception as exc:  # only a speed-up; never matters if it fails
        log.info("Warm-up skipped: %s", exc)


# --------------------------------------------------------------------------- simple pages
async def audit_page(request: Request):
    c = need_ws(request)
    rows = S.db.all("SELECT a.*, d.title AS draft_title FROM audit a LEFT JOIN drafts d ON d.id = a.draft_id "
                    "WHERE a.workspace_id = ? ORDER BY a.id DESC LIMIT 300", (c.ws,))
    for r in rows:
        r["detail_obj"] = json.loads(r.get("detail") or "{}")
    return render(request, "audit.html", {"rows": rows})


async def about_page(request: Request):
    with S.db.as_system():
        calls = S.db.one("SELECT COUNT(*) AS n, SUM(ok) AS ok, SUM(cached) AS cached, AVG(ms) AS ms FROM ai_calls "
                         "WHERE cached = 0") or {}
        by_model = S.db.all("SELECT provider, model, COUNT(*) AS n, SUM(ok) AS ok, AVG(ms) AS ms FROM ai_calls "
                            "WHERE cached = 0 AND provider != 'none' GROUP BY provider, model ORDER BY n DESC LIMIT 12")
        recent_errors = S.db.all("SELECT purpose, error, created_at FROM ai_calls WHERE ok = 0 ORDER BY id DESC LIMIT 5")
    accuracy = None
    path = os.path.join(os.path.dirname(HERE), "data", "eval_results.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                accuracy = json.load(fh)
        except ValueError:
            accuracy = None
    return render(request, "about.html", {"calls": calls, "by_model": by_model, "recent_errors": recent_errors,
                                          "accuracy": accuracy, "jobs": S.ai.status(), "rls_note": S.db.rls_note,
                                          "auth_mode": "Supabase Auth" if auth.supabase_on() else "built-in accounts"})


async def healthz(request: Request):
    ok = True
    try:
        with S.db.as_system():
            S.db.scalar("SELECT 1 AS one")
    except Exception:
        ok = False
    return JSONResponse({"ok": ok, "storage": S.db.kind, "rls": S.db.rls, "ai": S.ai.label,
                         "auth": "supabase" if auth.supabase_on() else "built-in", "warning": S.db.warning},
                        status_code=200 if ok else 503)


class CachedStatic(StaticFiles):
    """Static files are versioned in their URLs (?v=...), so browsers may keep them for a day."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp


class SameOriginPosts:
    """Refuse form posts sent from another site (CSRF). Browsers send an Origin header on every POST."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            headers = dict(scope["headers"])
            origin = headers.get(b"origin", b"").decode("latin-1")
            hosts = {headers.get(h, b"").decode("latin-1") for h in (b"host", b"x-forwarded-host")}
            if origin and origin != "null" and urlsplit(origin).netloc not in hosts:
                log.warning("Refused cross-site POST to %s from %s", scope["path"], origin)
                await PlainTextResponse("Cross-site form posts are not allowed.", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def http_error(request: Request, exc: HTTPException):
    if request.url.path.endswith("/status"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", {"code": exc.status_code, "detail": exc.detail}, status=exc.status_code)


async def server_error(request: Request, exc: Exception):
    if type(exc).__name__ == "InsufficientPrivilege":  # PostgreSQL row-level security said no
        log.warning("Database refused %s %s for this role", request.method, request.url.path)
        return render(request, "error.html", {"code": 403, "detail": "The database refused this action for your role "
                                                                    "in this workspace."}, status=403)
    log.exception("Unhandled error on %s", request.url.path)
    try:
        return render(request, "error.html", {"code": 500, "detail": "Something went wrong on our side. Please try again; "
                                                                     "if it keeps happening, tell us what you clicked."},
                      status=500)
    except Exception:
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("Something went wrong. Please try again.", status_code=500)


routes = [
    Route("/", dr.home),
    # demo + accounts
    Route("/demo/as", acc.demo_as, methods=["POST"]),
    Route("/role", acc.demo_as, methods=["POST"]),  # Round 2 URL, kept working
    Route("/signin", acc.signin_page), Route("/signin", acc.signin, methods=["POST"]),
    Route("/signup", acc.signup_page), Route("/signup", acc.signup, methods=["POST"]),
    Route("/signout", acc.signout, methods=["POST"]),
    Route("/forgot", acc.forgot_page), Route("/forgot", acc.forgot, methods=["POST"]),
    Route("/reset", acc.reset_page), Route("/reset", acc.reset, methods=["POST"]),
    Route("/workspaces", acc.workspaces_page),
    Route("/workspaces", acc.workspace_create, methods=["POST"]),
    Route("/workspaces/switch", acc.workspace_switch, methods=["POST"]),
    Route("/team", acc.team_page), Route("/team", acc.team_save, methods=["POST"]),
    # drafts
    Route("/drafts/new", dr.new_draft),
    Route("/drafts", dr.create_draft, methods=["POST"]),
    Route("/samples/{name}", dr.create_sample, methods=["POST"]),
    Route("/drafts/{id:int}", dr.draft_page),
    Route("/drafts/{id:int}/status", dr.draft_status),
    Route("/drafts/{id:int}/v/{n:int}/image", dr.version_image),
    Route("/drafts/{id:int}/findings/{key}", dr.decide, methods=["POST"]),
    Route("/drafts/{id:int}/discard", dr.discard_working, methods=["POST"]),
    Route("/drafts/{id:int}/recheck", dr.recheck, methods=["POST"]),
    Route("/drafts/{id:int}/fix-all", dr.fix_all, methods=["POST"]),
    Route("/drafts/{id:int}/retry", dr.retry_check, methods=["POST"]),
    Route("/drafts/{id:int}/submit", dr.submit, methods=["POST"]),
    Route("/drafts/{id:int}/review", dr.review, methods=["POST"]),
    Route("/drafts/{id:int}/comments", dr.add_comment, methods=["POST"]),
    Route("/drafts/{id:int}/record.pdf", dr.approval_record),
    Route("/drafts/{id:int}/reopen", insights.expiry_reopen, methods=["POST"]),
    Route("/review", dr.review_queue),
    # brand kit
    Route("/kit", kit.kit_page),
    Route("/kit/restore-demo", kit.restore_demo, methods=["POST"]),
    Route("/kit/rules/{rule_id}", kit.rule_page),
    Route("/kit/rules/{rule_id}/test", kit.rule_test, methods=["POST"]),
    Route("/kit/rules/{rule_id}/restore", kit.rule_restore, methods=["POST"]),
    Route("/kit/rules/{rule_id}/clarify", insights.clarify, methods=["POST"]),
    Route("/kit/{table}/save", kit.kit_save, methods=["POST"]),
    Route("/kit/{table}/delete", kit.kit_delete, methods=["POST"]),
    Route("/kit/{table}/import", kit.kit_import, methods=["POST"]),
    Route("/kit/{table}.csv", kit.kit_export),
    # improvement 1: guideline import
    Route("/guidelines", guidelines.guidelines_page),
    Route("/guidelines", guidelines.guideline_upload, methods=["POST"]),
    Route("/guidelines/{gid:int}", guidelines.guideline_page),
    Route("/guidelines/{gid:int}/status", guidelines.guideline_status),
    Route("/guidelines/{gid:int}/decide", guidelines.candidate_decide, methods=["POST"]),
    # improvement 2: insights
    Route("/insights", insights.insights_page),
    Route("/insights/reword/{cid:int}", insights.reword_decide, methods=["POST"]),
    # other
    Route("/audit", audit_page),
    Route("/about", about_page),
    Route("/healthz", healthz),
    Mount("/static", CachedStatic(directory=os.path.join(HERE, "static")), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan,
                middleware=[Middleware(GZipMiddleware, minimum_size=800), Middleware(SameOriginPosts),
                            Middleware(SessionMiddleware)],
                exception_handlers={HTTPException: http_error, 500: server_error}, max_body_size=10 * 1024 * 1024)
