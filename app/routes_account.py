"""Sign-in, sign-up, the demo switcher, workspaces and the team page."""
from __future__ import annotations

import re

from starlette.exceptions import HTTPException
from starlette.requests import Request

from . import auth
from .auth import AuthError
from .db import now_iso
from .seed import seed_kit_rows
from .web import S, clear_session, forget, ctx_of, go, need_role, read_form, render, safe_back, set_session

KITS = {"starter": "Starter rules (claims, tone, accessibility, confidentiality) to adapt",
        "demo": "A copy of the Paisaleaf demo kit, to explore with sample rules and facts",
        "empty": "Empty: I'll import our guideline or add rules myself"}


def _client(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))


def _site(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


# --------------------------------------------------------------------------- demo
async def demo_as(request: Request):
    form = await read_form(request)
    role = form.get("role") if form.get("role") in auth.DEMO_PEOPLE else "creator"
    uid = auth.demo_user_id(S.db, role)
    if not uid or not S.demo_ws:
        raise HTTPException(503, "The demo workspace is not ready yet. Refresh in a moment.")
    set_session(request, uid, S.demo_ws)
    c = ctx_of(request)
    back = safe_back(form.get("back"))
    if c.ws != S.demo_ws:
        back = "/"
    person = auth.get_user(S.db, uid)
    return go(back, f"You are now {person['name']} ({auth.ROLES[role]['label']}) in the demo workspace.")


# --------------------------------------------------------------------------- sign in / up / out
async def signin_page(request: Request):
    return render(request, "signin.html", {"mode": "signin", "email": request.query_params.get("email", "")})


async def signin(request: Request):
    form = await read_form(request)
    email_raw = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    if not S.throttle.allow("in:" + _client(request)) or not S.throttle.allow("in:" + email_raw):
        return go("/signin", "Too many sign-in attempts. Wait five minutes and try again.", "error")
    demo_role = next((r for r, p in auth.DEMO_PEOPLE.items() if p["email"] == email_raw), None)
    if demo_role:  # the public demo logins shown on the sign-in page
        if password != auth.DEMO_PASSWORD:
            return go(f"/signin?email={email_raw}", f"The demo password is {auth.DEMO_PASSWORD}.", "error")
        set_session(request, auth.demo_user_id(S.db, demo_role), S.demo_ws)
        return go("/", f"Signed in to the demo as {auth.DEMO_PEOPLE[demo_role]['name']}.")
    try:
        email = auth.clean_email(email_raw)
        if not password:
            raise AuthError("Enter your password.")
        user = auth.find_user(S.db, email)
        if auth.supabase_on():
            auth_id = auth.supabase_sign_in(email, password)
            if not user:  # signed up through Supabase but the profile row is missing (for example a fresh database)
                uid = auth.create_user(S.db, email, email.split("@")[0].title(), "supabase", auth_id=auth_id)
                user = auth.get_user(S.db, uid)
        else:
            if not user or user.get("auth") != "local" or not auth.verify_password(password, user.get("password_hash")):
                raise AuthError("Wrong email or password.")
    except AuthError as exc:
        return go(f"/signin?email={email_raw[:120]}", str(exc), "error")
    auth.attach_invites(S.db, user)
    mems = auth.memberships(S.db, user["id"])
    real = [m for m in mems if not m["is_demo"]]
    ws = next((m["workspace_id"] for m in real if m["workspace_id"] == user.get("last_workspace")),
              real[0]["workspace_id"] if real else None)
    set_session(request, user["id"], ws)
    if not ws:
        return go("/workspaces", f"Welcome, {user['name']}. Create your workspace to start.")
    return go("/", f"Signed in as {user['name']}.")


async def signup_page(request: Request):
    return render(request, "signin.html", {"mode": "signup", "kits": KITS, "email": ""})


async def signup(request: Request):
    form = await read_form(request)
    name = re.sub(r"\s+", " ", str(form.get("name") or "")).strip()[:60]
    email_raw = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    ws_name = str(form.get("workspace") or "").strip()
    kit = form.get("kit") if form.get("kit") in KITS else "starter"
    if not S.throttle.allow("up:" + _client(request)):
        return go("/signup", "Too many attempts from this network. Wait five minutes and try again.", "error")
    try:
        email = auth.clean_email(email_raw)
        if len(name) < 2:
            raise AuthError("Enter your name.")
        auth.check_new_password(password)
        if len(ws_name.strip()) < 2:
            raise AuthError("Give your workspace a name (for example your brand, club or team).")
        if auth.find_user(S.db, email):
            raise AuthError("An account with this email already exists. Sign in instead.")
        if auth.supabase_on():
            res = auth.supabase_sign_up(email, password, name, _site(request) + "/signin")
            uid = auth.create_user(S.db, email, name, "supabase", auth_id=res.get("auth_id"))
        else:
            uid = auth.create_user(S.db, email, name, "local", password=password)
            res = {"confirmed": True}
    except AuthError as exc:
        return go("/signup", str(exc), "error")
    user = auth.get_user(S.db, uid)
    auth.attach_invites(S.db, user)
    ws = auth.create_workspace(S.db, user, ws_name)
    with S.db.acting_as(uid, ws):
        seed_kit_rows(S.db, ws, kit, actor=name)
        S.db.audit("workspace.created", name, "brand", None, {"name": ws_name, "kit": kit})
    if not res.get("confirmed"):
        clear_session(request)
        return go(f"/signin?email={email}", "Account created. Check your inbox to confirm your email, then sign in.")
    set_session(request, uid, ws)
    return go("/", f"Welcome, {name}! Your workspace “{ws_name}” is ready. You are its brand manager.")


async def signout(request: Request):
    clear_session(request)
    return go("/signin", "Signed out. You can still explore the public demo.")


async def forgot_page(request: Request):
    return render(request, "signin.html", {"mode": "forgot", "email": ""})


async def forgot(request: Request):
    form = await read_form(request)
    email_raw = (form.get("email") or "").strip().lower()
    if not S.throttle.allow("reset:" + _client(request)):
        return go("/forgot", "Too many attempts. Wait five minutes and try again.", "error")
    if not auth.supabase_on():
        return go("/forgot", "Password reset by email needs Supabase Auth. Ask the person who runs this Brandproof "
                             "server to reset your account.", "error")
    try:
        auth.supabase_send_reset(auth.clean_email(email_raw), _site(request) + "/reset")
    except AuthError as exc:
        return go("/forgot", str(exc), "error")
    return go("/signin", "If that email has an account, a reset link is on its way.")


async def reset_page(request: Request):
    return render(request, "signin.html", {"mode": "reset", "email": ""})


async def reset(request: Request):
    form = await read_form(request)
    token = str(form.get("access_token") or "")
    password = form.get("password") or ""
    try:
        if not auth.supabase_on() or not token:
            raise AuthError("Open the reset link from your email again.")
        auth.check_new_password(password)
        email = auth.supabase_set_password(token, password)
    except AuthError as exc:
        return go("/reset", str(exc), "error")
    return go(f"/signin?email={email}", "Password changed. Sign in with your new password.")


# --------------------------------------------------------------------------- workspaces
async def workspaces_page(request: Request):
    c = ctx_of(request)
    return render(request, "workspaces.html", {"kits": KITS, "signed_in": c.user.get("auth") != "demo"})


async def workspace_create(request: Request):
    c = ctx_of(request)
    if c.user.get("auth") == "demo":
        return go("/signup", "Create a free account first; the demo people can't own workspaces.")
    form = await read_form(request)
    kit = form.get("kit") if form.get("kit") in KITS else "starter"
    if len(auth.memberships(S.db, c.uid)) >= 10:
        return go("/workspaces", "You can be in at most 10 workspaces.", "error")
    try:
        ws = auth.create_workspace(S.db, c.user, str(form.get("name") or ""))
    except AuthError as exc:
        return go("/workspaces", str(exc), "error")
    with S.db.acting_as(c.uid, ws):
        seed_kit_rows(S.db, ws, kit, actor=c.name)
        S.db.audit("workspace.created", c.name, "brand", None, {"name": form.get("name"), "kit": kit})
    set_session(request, c.uid, ws)
    return go("/", "Workspace created. You are its brand manager.")


async def workspace_switch(request: Request):
    c = ctx_of(request)
    form = await read_form(request)
    try:
        ws = int(form.get("workspace") or 0)
    except ValueError:
        ws = 0
    if not auth.membership(S.db, c.uid, ws):
        return go("/workspaces", "You are not a member of that workspace.", "error")
    set_session(request, c.uid, ws)
    return go("/", "Switched workspace.")


# --------------------------------------------------------------------------- team
def team_rows(ws: int) -> list[dict]:
    with S.db.as_system():
        return S.db.all("SELECT m.id, m.email, m.role, m.title, m.user_id, m.created_at, u.name FROM members m "
                        "LEFT JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ? ORDER BY m.id", (ws,))


async def team_page(request: Request):
    c = ctx_of(request)
    if not c.ws:
        return go("/workspaces")
    return render(request, "team.html", {"members": team_rows(c.ws)})


async def team_save(request: Request):
    """Demo workspace: rename the demo people. Real workspaces: add, change or remove members."""
    c = need_role(request, "brand")
    forget()
    form = await read_form(request)
    action = form.get("action") or ("reset" if form.get("reset") == "1" else "rename")
    rows = team_rows(c.ws)
    if c.is_demo:
        if action == "reset":
            with S.db.as_system():
                for role, p in auth.DEMO_PEOPLE.items():
                    S.db.execute("UPDATE users SET name = ? WHERE email = ?", (p["name"], p["email"]))
                    S.db.execute("UPDATE members SET title = ? WHERE email = ? AND workspace_id = ?",
                                 (p["title"], p["email"], c.ws))
            S.db.audit("team.reset", c.name, c.role, None, {})
            return go("/team", "Names and titles reset to the defaults.")
        changes = {}
        for role, p in auth.DEMO_PEOPLE.items():
            name = re.sub(r"\s+", " ", str(form.get(f"{role}_name") or "")).strip()[:40]
            title = re.sub(r"\s+", " ", str(form.get(f"{role}_label") or "")).strip()[:40]
            if not name or not title:
                return go("/team", "Every person needs a name and a title.", "error")
            changes[role] = (name, title)
        with S.db.as_system():
            for role, (name, title) in changes.items():
                email = auth.DEMO_PEOPLE[role]["email"]
                S.db.execute("UPDATE users SET name = ? WHERE email = ?", (name, email))
                S.db.execute("UPDATE members SET title = ? WHERE email = ? AND workspace_id = ?", (title, email, c.ws))
        S.db.audit("team.updated", c.name, c.role, None, {k: f"{v[0]} ({v[1]})" for k, v in changes.items()})
        return go("/team", "Saved. The new names and titles show everywhere straight away.")

    brands = [r for r in rows if r["role"] == "brand"]
    if action == "invite":
        try:
            email = auth.clean_email(form.get("email") or "")
        except AuthError as exc:
            return go("/team", str(exc), "error")
        role = form.get("role") if form.get("role") in auth.ROLES else "creator"
        title = re.sub(r"\s+", " ", str(form.get("title") or "")).strip()[:40] or auth.ROLES[role]["label"]
        if any(r["email"] == email for r in rows):
            return go("/team", "That person is already in this workspace.", "error")
        if len(rows) >= 50:
            return go("/team", "A workspace can have up to 50 members.", "error")
        user = auth.find_user(S.db, email)
        S.db.execute("INSERT INTO members (workspace_id, user_id, email, role, title, added_by, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (c.ws, user["id"] if user else None, email, role, title, c.name, now_iso()))
        S.db.audit("member.invited", c.name, c.role, None, {"email": email, "role": role})
        note = (" They can switch to this workspace now." if user else
                " Ask them to create a free account with this email; they'll join automatically.")
        return go("/team", f"Added {email} as {auth.ROLES[role]['label']}.{note}")
    try:
        mid = int(form.get("member") or 0)
    except ValueError:
        mid = 0
    target = next((r for r in rows if r["id"] == mid), None)
    if not target:
        return go("/team", "That member is no longer in this workspace.", "error")
    if action == "update":
        role = form.get("role") if form.get("role") in auth.ROLES else target["role"]
        title = re.sub(r"\s+", " ", str(form.get("title") or "")).strip()[:40] or auth.ROLES[role]["label"]
        if target["role"] == "brand" and role != "brand" and len(brands) <= 1:
            return go("/team", "A workspace needs at least one brand manager. Make someone else brand manager first.", "error")
        S.db.execute("UPDATE members SET role = ?, title = ? WHERE id = ? AND workspace_id = ?", (role, title, mid, c.ws))
        S.db.audit("member.updated", c.name, c.role, None, {"email": target["email"], "role": role})
        return go("/team", f"Updated {target['name'] or target['email']}.")
    if action == "remove":
        if target["role"] == "brand" and len(brands) <= 1:
            return go("/team", "You can't remove the last brand manager.", "error")
        S.db.execute("DELETE FROM members WHERE id = ? AND workspace_id = ?", (mid, c.ws))
        S.db.audit("member.removed", c.name, c.role, None, {"email": target["email"]})
        return go("/team", f"Removed {target['name'] or target['email']}.")
    raise HTTPException(400, "Unknown action")
