"""Drafts: create, check (in the background), fix, re-check, submit, review, comment, approval record."""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets

from markupsafe import Markup, escape
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .checks.image import normalise_image
from .checks.pipeline import run_checks
from .db import now_iso
from .kit import load_kit
from .seed import SAMPLE_EMAIL, SAMPLE_POST
from .textutil import find_span, tidy_spaces, word_diff_html
from .web import (CHANNELS, EDITABLE, HERE, MAX_TEXT, S, ctx_of, docx_text, go, need_role, need_ws, parse_locked,
                  pdf_pages, read_form, read_upload, render)

log = logging.getLogger("brandproof.drafts")


# --------------------------------------------------------------------------- data access (always scoped to the workspace)
def get_draft(ws: int, draft_id: int) -> dict:
    d = S.db.one("SELECT * FROM drafts WHERE id = ? AND workspace_id = ?", (draft_id, ws))
    if not d:
        raise HTTPException(404, "Draft not found in this workspace.")
    d["locked_list"] = json.loads(d.get("locked") or "[]")
    return d


def get_version(ws: int, draft_id: int, n: int) -> dict | None:
    v = S.db.one("SELECT * FROM versions WHERE draft_id = ? AND n = ? AND workspace_id = ?", (draft_id, n, ws))
    if v:
        v["findings_list"] = json.loads(v.get("findings") or "[]")
        v["summary_obj"] = json.loads(v.get("summary") or "{}")
        v["pages_list"] = json.loads(v.get("pages") or "[]")
    return v


def image_bytes(v: dict | None) -> bytes | None:
    return base64.b64decode(v["image"]) if v and v.get("image") else None


def decisions_for(ws: int, draft_id: int, n: int) -> dict[str, dict]:
    rows = S.db.all("SELECT * FROM decisions WHERE draft_id = ? AND version_n = ? AND workspace_id = ? ORDER BY id",
                    (draft_id, n, ws))
    return {r["finding_key"]: r for r in rows}


# --------------------------------------------------------------------------- the background check
def queue_check(ws: int, draft_id: int, n: int) -> None:
    S.jobs.submit(f"check:{draft_id}:{n}", run_version, ws, draft_id, n)


def run_version(ws: int, draft_id: int, n: int) -> None:
    """Run every check on a saved version (state 'checking') and store the result. Runs in a background thread."""
    v = get_version(ws, draft_id, n)
    if not v or v.get("state") != "checking":
        return
    draft = get_draft(ws, draft_id)
    try:
        kit = load_kit(S.db, ws)
        meta = {"channel": draft.get("channel"), "goal": draft.get("goal"), "locked": draft["locked_list"]}
        result = run_checks(S.ai, kit, meta, v.get("body") or "", image_bytes(v), v.get("image_mime"),
                            v.get("alt_text") or "", v["pages_list"] or None)
    except Exception as exc:
        log.exception("check failed for draft %s v%s", draft_id, n)
        S.db.execute("UPDATE versions SET state = 'failed', error = ? WHERE draft_id = ? AND n = ? AND workspace_id = ?",
                     (f"{type(exc).__name__}: {str(exc)[:200]}", draft_id, n, ws))
        S.db.audit("check.failed", v.get("created_by") or "System", "creator", draft_id, {"version": n})
        return
    S.db.execute(
        "UPDATE versions SET findings = ?, summary = ?, kit_hash = ?, state = 'done', error = NULL "
        "WHERE draft_id = ? AND n = ? AND workspace_id = ?",
        (json.dumps(result["findings"], ensure_ascii=False), json.dumps(result["summary"], ensure_ascii=False),
         kit.hash, draft_id, n, ws))
    S.db.execute("UPDATE drafts SET updated_at = ? WHERE id = ? AND workspace_id = ?", (now_iso(), draft_id, ws))
    carried = carry_rejections(ws, draft_id, n - 1, n, result["findings"]) if n > 1 else 0
    s = result["summary"]
    S.db.audit("check.completed", v.get("created_by") or "System", "creator", draft_id, {
        "version": n, "issues": s["issues"], "blockers": s["counts"]["blocker"], "ai": s["ai"]["label"],
        "ai_calls_ok": sum(1 for c in s["ai"]["calls"] if c.get("ok")), "ms": s["ms"], "carried": carried,
        "masked": s.get("masked", {}).get("total", 0)})


def recover_checks() -> int:
    """After a restart, finish checks that were interrupted."""
    with S.db.as_system():
        rows = S.db.all("SELECT workspace_id, draft_id, n, created_uid FROM versions WHERE state = 'checking'")
    for r in rows:
        with S.db.acting_as(r["created_uid"], r["workspace_id"]):
            queue_check(r["workspace_id"], r["draft_id"], r["n"])
    return len(rows)


def carry_rejections(ws: int, draft_id: int, old_n: int, new_n: int, findings: list[dict]) -> int:
    """Keep a creator's 'reject with reason' decisions when the same issue shows up again after a re-check."""
    old = S.db.all("SELECT * FROM decisions WHERE draft_id = ? AND version_n = ? AND action = 'reject' AND workspace_id = ?",
                   (draft_id, old_n, ws))
    carried = 0
    for f in findings:
        for d in old:
            if d["rule_id"] == f["rule_id"] and (d.get("before_text") or "").lower() == (f.get("quote") or f["title"]).lower():
                S.db.execute(
                    """INSERT INTO decisions (workspace_id, draft_id, version_n, finding_key, rule_id, severity, action,
                       reason, before_text, after_text, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, 'reject', ?, ?, '', ?, ?)
                       ON CONFLICT (draft_id, version_n, finding_key) DO NOTHING""",
                    (ws, draft_id, new_n, f["key"], f["rule_id"], f["severity"], d["reason"], d["before_text"],
                     d["actor"], now_iso()))
                carried += 1
                break
    return carried


def new_version(c, draft: dict, n: int, body: str, image: bytes | None, mime: str | None, alt: str,
                source_name: str, pages: list | None) -> None:
    """Save the input of version n (state 'checking') and start the check in the background."""
    S.db.execute(
        """INSERT INTO versions (workspace_id, draft_id, n, body, image, image_mime, alt_text, source_name, pages, state,
           created_by, created_uid, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'checking', ?, ?, ?)""",
        (c.ws, draft["id"], n, body, base64.b64encode(image).decode() if image else None, mime, alt, source_name,
         json.dumps(pages) if pages else None, c.name, c.uid, now_iso()))
    S.db.execute(
        """UPDATE drafts SET current_version = ?, working_body = ?, working_alt = ?, working_dirty = 0,
           status = 'checked', updated_at = ? WHERE id = ? AND workspace_id = ?""",
        (n, body, alt, now_iso(), draft["id"], c.ws))
    queue_check(c.ws, draft["id"], n)


def join_pages(pages: list[str]) -> tuple[str, list[list[int]]]:
    """Join PDF pages into one text and remember where each page starts and ends."""
    parts, spans, pos = [], [], 0
    for p in pages:
        p = p.strip()
        parts.append(p)
        spans.append([pos, pos + len(p)])
        pos += len(p) + 2
    return "\n\n".join(parts), spans


# --------------------------------------------------------------------------- rendering helpers
def highlight(body: str, findings: list[dict]) -> Markup:
    marks = [f for f in findings if f["target"] == "text" and f.get("start") is not None and f.get("end")]
    if not body:
        return Markup("")
    points = sorted({0, len(body), *[m["start"] for m in marks], *[m["end"] for m in marks]})
    rank = {"blocker": 0, "major": 1, "minor": 2, "info": 3, "ok": 4}
    out = []
    for a, b in zip(points, points[1:]):
        if a >= b:
            continue
        seg = str(escape(body[a:b])).replace("\n", "<br>")
        cover = [m for m in marks if m["start"] <= a and m["end"] >= b]
        if not cover:
            out.append(seg)
            continue
        top = min(cover, key=lambda m: rank.get(m["severity"], 9))
        keys = " ".join(m["key"] for m in cover)
        nums = [str(m["n"]) for m in cover if m["end"] == b and m.get("n")]
        sup = f"<sup>{','.join(nums)}</sup>" if nums else ""
        out.append(f'<mark class="hl hl-{top["severity"]}" data-keys="{keys}">{seg}{sup}</mark>')
    return Markup("".join(out))


def apply_fix(text: str, fix: dict, replacement: str | None, locked: list[str]) -> tuple[str | None, str]:
    """Apply a fix to the working copy. Returns (new text or None, message)."""
    kind = fix.get("type")
    if kind == "append":
        addition = (replacement if replacement is not None else fix.get("text", "")).strip()
        if addition and addition.lower() in text.lower():
            return text, "Already there"
        return (text.rstrip() + ("\n" if "\n" in text else " ") + addition).strip(), "Added"
    if kind in ("replace", "remove"):
        find = fix.get("find") or ""
        span = find_span(text, find)
        if not span:
            return None, "That text has already changed in your working copy."
        new = replacement if replacement is not None else fix.get("text", "")
        result = text[:span[0]] + new + text[span[1]:]
        if kind == "remove" or not new.strip():
            result = tidy_spaces(result)
        for phrase in locked:
            if phrase and phrase.lower() in text.lower() and phrase.lower() not in result.lower():
                return None, f"This change would remove your locked phrase “{phrase}”. Use Edit to keep it."
        return result, "Applied"
    return text, "Noted"


# --------------------------------------------------------------------------- pages
async def home(request: Request):
    c = ctx_of(request)
    if not c.ws:
        return go("/workspaces", "Create a workspace to start.")
    drafts = S.db.all("SELECT * FROM drafts WHERE workspace_id = ? ORDER BY updated_at DESC, id DESC LIMIT 200", (c.ws,))
    if drafts:
        ids = [d["id"] for d in drafts]
        marks = ",".join("?" for _ in ids)
        rows = S.db.all(f"SELECT draft_id, n, state, summary FROM versions WHERE workspace_id = ? AND draft_id IN ({marks})",
                        (c.ws, *ids))
        by = {(r["draft_id"], r["n"]): r for r in rows}
        for d in drafts:
            v = by.get((d["id"], d["current_version"])) or {}
            summary = json.loads(v.get("summary") or "{}")
            d["state"] = v.get("state") or "done"
            d["counts"] = summary.get("counts") or {}
            d["issues"] = summary.get("issues") or 0
    return render(request, "index.html", {"drafts": drafts})


async def new_draft(request: Request):
    need_ws(request)
    return render(request, "new.html", {"channels": CHANNELS, "token": secrets.token_urlsafe(16)})


def _existing_by_token(ws: int, token: str) -> int | None:
    if not token:
        return None
    return S.db.scalar("SELECT id FROM drafts WHERE workspace_id = ? AND client_token = ?", (ws, token))


async def create_draft(request: Request):
    c = need_role(request, "creator", "brand")
    form = await read_form(request)
    token = (form.get("token") or "").strip()[:64]
    dup = _existing_by_token(c.ws, token)
    if dup:  # the same form was sent twice (double click, refresh, retry)
        return go(f"/drafts/{dup}")
    title = (form.get("title") or "").strip()[:140] or "Untitled draft"
    body = (form.get("body") or "").replace("\r\n", "\n").strip()
    source_name, pages = "", None
    doc = form.get("doc") or form.get("docx")
    data = await read_upload(doc)
    if data:
        name = (doc.filename or "").lower()
        if name.endswith(".pdf") or data[:5] == b"%PDF-":
            body, pages = join_pages(pdf_pages(data))
        elif name.endswith(".docx") or data[:2] == b"PK":
            body = docx_text(data)
        else:
            return go("/drafts/new", "Upload a PDF or a Word (.docx) file, or paste the text.", "error")
        source_name = doc.filename
    img_data = await read_upload(form.get("image"))
    image, mime = None, None
    if img_data:
        try:
            image, mime, _ = normalise_image(img_data)
        except Exception:
            return go("/drafts/new", "That image could not be read. Use PNG, JPG or WebP.", "error")
    if not body.strip() and not image:
        return go("/drafts/new", "Add some text, a PDF or Word file, or an image to check.", "error")
    truncated = len(body) > MAX_TEXT
    body = body[:MAX_TEXT]
    alt = (form.get("alt") or "").strip()[:500]
    locked = parse_locked(form.get("locked") or "")
    channel = form.get("channel") if form.get("channel") in CHANNELS else "Other"
    ts = now_iso()
    try:
        draft_id = S.db.insert(
            """INSERT INTO drafts (workspace_id, title, channel, goal, locked, status, created_by, created_at, updated_at,
               current_version, working_body, working_alt, working_dirty, client_token)
               VALUES (?, ?, ?, ?, ?, 'checked', ?, ?, ?, 0, ?, ?, 0, ?) RETURNING id""",
            (c.ws, title, channel, (form.get("goal") or "").strip()[:300], json.dumps(locked), c.name, ts, ts, body, alt,
             token or None))
    except Exception:
        dup = _existing_by_token(c.ws, token)
        if dup:
            return go(f"/drafts/{dup}")
        raise
    S.db.audit("draft.created", c.name, c.role, draft_id, {"title": title, "file": source_name, "image": bool(image),
                                                           "pages": len(pages) if pages else 0})
    draft = get_draft(c.ws, draft_id)
    new_version(c, draft, 1, body, image, mime, alt, source_name, pages)
    msg = f"Only the first {MAX_TEXT:,} characters were kept." if truncated else ""
    return go(f"/drafts/{draft_id}", msg, "error" if truncated else "ok")


async def create_sample(request: Request):
    c = need_role(request, "creator", "brand")
    name = request.path_params["name"]
    sample = SAMPLE_POST if name == "instagram" else SAMPLE_EMAIL if name == "email" else None
    if not sample:
        raise HTTPException(404, "Unknown sample")
    image, mime = None, None
    if sample.get("image"):
        with open(os.path.join(HERE, "static", sample["image"]), "rb") as fh:
            image, mime, _ = normalise_image(fh.read())
    ts = now_iso()
    draft_id = S.db.insert(
        """INSERT INTO drafts (workspace_id, title, channel, goal, locked, status, created_by, created_at, updated_at,
           current_version, working_body, working_alt, working_dirty) VALUES (?, ?, ?, ?, ?, 'checked', ?, ?, ?, 0, ?, ?, 0)
           RETURNING id""",
        (c.ws, sample["title"], sample["channel"], sample["goal"], json.dumps(sample["locked"]), c.name, ts, ts,
         sample["body"], sample["alt"]))
    S.db.audit("draft.created", c.name, c.role, draft_id, {"title": sample["title"], "sample": name})
    new_version(c, get_draft(c.ws, draft_id), 1, sample["body"], image, mime, sample["alt"], "", None)
    return go(f"/drafts/{draft_id}")


async def draft_status(request: Request):
    """Small JSON endpoint the checking page polls."""
    c = need_ws(request)
    draft = get_draft(c.ws, int(request.path_params["id"]))
    v = S.db.one("SELECT state, error FROM versions WHERE draft_id = ? AND n = ? AND workspace_id = ?",
                 (draft["id"], draft["current_version"], c.ws)) or {}
    return JSONResponse({"state": v.get("state") or "missing", "version": draft["current_version"]},
                        headers={"Cache-Control": "no-store"})


async def draft_page(request: Request):
    c = need_ws(request)
    draft_id = int(request.path_params["id"])
    draft = get_draft(c.ws, draft_id)
    if draft["current_version"] < 1:
        raise HTTPException(404, "This draft has no checked version yet.")
    try:
        n = int(request.query_params.get("v") or draft["current_version"])
    except ValueError:
        n = draft["current_version"]
    n = max(1, min(n, draft["current_version"]))
    version = get_version(c.ws, draft_id, n)
    if not version:
        raise HTTPException(404, "Version not found")
    is_current = n == draft["current_version"]
    if version.get("state") in ("checking", "failed"):
        if version["state"] == "checking" and not S.jobs.is_running(f"check:{draft_id}:{n}"):
            queue_check(c.ws, draft_id, n)  # e.g. the server restarted while this was checking
        return render(request, "checking.html", {"draft": draft, "version": version, "n": n})
    findings = version["findings_list"]
    decisions = decisions_for(c.ws, draft_id, n)
    issues = [f for f in findings if f["severity"] != "ok"]
    supported = [f for f in findings if f["severity"] == "ok"]
    open_blockers = [f for f in issues if f["severity"] == "blocker" and f["key"] not in decisions]
    versions = S.db.all("SELECT n, created_by, created_at, summary, state FROM versions WHERE draft_id = ? AND workspace_id = ? "
                        "ORDER BY n DESC", (draft_id, c.ws))
    for v in versions:
        s = json.loads(v["summary"] or "{}")
        v["issues"] = s.get("issues", 0)
        v["blockers"] = (s.get("counts") or {}).get("blocker", 0)
    prev = get_version(c.ws, draft_id, n - 1) if n > 1 else None
    diff = Markup(word_diff_html(prev["body"] or "", version["body"] or "")) if prev else None
    working_diff = None
    if is_current and draft["working_dirty"]:
        working_diff = Markup(word_diff_html(version["body"] or "", draft["working_body"] or ""))
    comments = S.db.all("SELECT * FROM comments WHERE draft_id = ? AND workspace_id = ? ORDER BY id", (draft_id, c.ws))
    audit = S.db.all("SELECT * FROM audit WHERE draft_id = ? AND workspace_id = ? ORDER BY id DESC LIMIT 60", (draft_id, c.ws))
    for a in audit:
        a["detail_obj"] = json.loads(a.get("detail") or "{}")
    approval = None
    if draft["status"] == "approved":
        approval = S.db.one("SELECT * FROM audit WHERE draft_id = ? AND workspace_id = ? AND action IN "
                            "('review.approved', 'draft.auto_approved') ORDER BY id DESC", (draft_id, c.ws))
        if approval:
            approval["detail_obj"] = json.loads(approval.get("detail") or "{}")
    ctx = {
        "draft": draft, "version": version, "n": n, "is_current": is_current, "findings": issues,
        "supported": supported, "decisions": decisions, "open_blockers": open_blockers, "versions": versions,
        "diff": diff, "working_diff": working_diff, "comments": comments, "audit": audit, "approval": approval,
        "body_html": highlight(version["body"] or "", findings),
        "can_edit": is_current and draft["status"] in EDITABLE and c.role in ("creator", "brand"),
        "can_review": is_current and draft["status"] == "in_review" and c.role in ("reviewer", "brand"),
        "summary": version["summary_obj"], "channels": CHANNELS,
    }
    return render(request, "draft.html", ctx)


async def version_image(request: Request):
    c = need_ws(request)
    v = get_version(c.ws, int(request.path_params["id"]), int(request.path_params["n"]))
    data = image_bytes(v)
    if not data:
        raise HTTPException(404, "No image")
    return Response(data, media_type=v.get("image_mime") or "image/png", headers={"Cache-Control": "private, max-age=86400"})


def _current_done(c, draft: dict) -> dict:
    version = get_version(c.ws, draft["id"], draft["current_version"])
    if not version or version.get("state") != "done":
        raise HTTPException(409, "This version is still being checked. Wait a few seconds and try again.")
    return version


async def decide(request: Request):
    c = need_role(request, "creator", "brand")
    draft_id = int(request.path_params["id"])
    key = request.path_params["key"]
    draft = get_draft(c.ws, draft_id)
    anchor = f"/drafts/{draft_id}#f-{key}"
    if draft["status"] not in EDITABLE:
        return go(f"/drafts/{draft_id}", "This draft is locked while it is in review or approved.", "error")
    version = _current_done(c, draft)
    finding = next((f for f in version["findings_list"] if f["key"] == key), None)
    if not finding:
        return go(f"/drafts/{draft_id}", "That finding belongs to an older version. Here is the latest one.", "error")
    form = await read_form(request)
    action = form.get("action")
    if decisions_for(c.ws, draft_id, draft["current_version"]).get(key):
        return go(anchor, "You already decided on this finding.", "error")
    before = finding.get("quote") or finding["title"]
    after = ""
    fix = finding.get("fix") or {"type": "advice", "text": ""}
    if action == "reject":
        reason = (form.get("reason") or "").strip()[:500]
        if len(reason) < 3:
            return go(anchor, "Please give a short reason for rejecting this finding.", "error")
        msg = "Rejected. Your reason is saved and shown to the reviewer."
    elif action in ("accept", "edit"):
        reason = ""
        replacement = form.get("text") if action == "edit" else None
        if replacement is not None:
            replacement = replacement.strip()[:2000]
        if fix["type"] == "alt":
            new_alt = (replacement if replacement is not None else fix.get("text", ""))[:500]
            S.db.execute("UPDATE drafts SET working_alt = ?, working_dirty = 1, updated_at = ? WHERE id = ? AND workspace_id = ?",
                         (new_alt, now_iso(), draft_id, c.ws))
            after = new_alt
            msg = "Alt text added to your working copy. Re-check to confirm."
        elif fix["type"] in ("replace", "remove", "append"):
            new_text, msg = apply_fix(draft["working_body"] or "", fix, replacement, draft["locked_list"])
            if new_text is None:
                return go(anchor, msg, "error")
            after = replacement if replacement is not None else fix.get("text", "")
            S.db.execute("UPDATE drafts SET working_body = ?, working_dirty = 1, updated_at = ? WHERE id = ? AND workspace_id = ?",
                         (new_text[:MAX_TEXT], now_iso(), draft_id, c.ws))
            msg = f"{msg} to your working copy. Re-check when you're ready."
        else:
            action = "acknowledge"
            msg = "Noted. Fix it in the design, then upload the new image when you re-check."
    else:
        raise HTTPException(400, "Unknown action")
    try:
        S.db.execute(
            """INSERT INTO decisions (workspace_id, draft_id, version_n, finding_key, rule_id, severity, action, reason,
               before_text, after_text, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (c.ws, draft_id, draft["current_version"], key, finding["rule_id"], finding["severity"], action, reason, before,
             after, c.name, now_iso()))
    except Exception:
        if decisions_for(c.ws, draft_id, draft["current_version"]).get(key):
            return go(anchor, "You already decided on this finding.", "error")
        raise
    S.db.audit(f"finding.{action}", c.name, c.role, draft_id, {
        "version": draft["current_version"], "rule": finding["rule_id"], "finding": finding["title"],
        "reason": reason, "after": after[:200]})
    if action in ("accept", "edit") and fix["type"] in ("replace", "remove") and finding.get("start") is not None:
        # findings inside the text that was just replaced are resolved by this fix
        decided = decisions_for(c.ws, draft_id, draft["current_version"])
        superseded = 0
        for other in version["findings_list"]:
            if (other["key"] == key or other["key"] in decided or other.get("target") != "text"
                    or other.get("start") is None or other["severity"] == "ok"
                    or (other.get("fix") or {}).get("type") not in ("replace", "remove")):
                continue
            if finding["start"] <= other["start"] and other["end"] <= finding["end"]:
                S.db.execute(
                    """INSERT INTO decisions (workspace_id, draft_id, version_n, finding_key, rule_id, severity, action,
                       reason, before_text, after_text, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, 'superseded', ?, ?, '', ?, ?)
                       ON CONFLICT (draft_id, version_n, finding_key) DO NOTHING""",
                    (c.ws, draft_id, draft["current_version"], other["key"], other["rule_id"], other["severity"],
                     f"Resolved by the fix for finding {finding.get('n') or ''}", other.get("quote") or other["title"],
                     c.name, now_iso()))
                superseded += 1
        if superseded:
            msg += f" {superseded} overlapping finding(s) were resolved by the same fix."
    return go(anchor, msg)


async def discard_working(request: Request):
    c = need_role(request, "creator", "brand")
    draft_id = int(request.path_params["id"])
    draft = get_draft(c.ws, draft_id)
    if draft["status"] not in EDITABLE:
        return go(f"/drafts/{draft_id}", "This draft is locked while it is in review or approved.", "error")
    version = _current_done(c, draft)
    S.db.execute("UPDATE drafts SET working_body = ?, working_alt = ?, working_dirty = 0 WHERE id = ? AND workspace_id = ?",
                 (version["body"], version["alt_text"], draft_id, c.ws))
    S.db.execute("DELETE FROM decisions WHERE draft_id = ? AND version_n = ? AND workspace_id = ? "
                 "AND action IN ('accept', 'edit', 'superseded')", (draft_id, draft["current_version"], c.ws))
    S.db.audit("working.discarded", c.name, c.role, draft_id, {"version": draft["current_version"]})
    return go(f"/drafts/{draft_id}", "Working copy reset to the last checked version.")


async def recheck(request: Request):
    c = need_role(request, "creator", "brand")
    draft_id = int(request.path_params["id"])
    draft = get_draft(c.ws, draft_id)
    if draft["status"] not in EDITABLE:
        return go(f"/drafts/{draft_id}", "This draft is locked while it is in review or approved.", "error")
    form = await read_form(request)
    base = form.get("base")
    if base and str(base) != str(draft["current_version"]):  # sent twice, or from an old tab
        return go(f"/drafts/{draft_id}", f"Already re-checked: this is version {draft['current_version']}.")
    old = _current_done(c, draft)
    body = form.get("body")
    body = (body if body is not None else draft["working_body"] or "").replace("\r\n", "\n").strip()[:MAX_TEXT]
    alt = form.get("alt")
    alt = (alt if alt is not None else draft["working_alt"] or "").strip()[:500]
    image, mime = image_bytes(old), old.get("image_mime")
    new_img = await read_upload(form.get("image"))
    if not new_img and form.get("sample_image") == "fixed" and c.is_demo:  # demo shortcut: the corrected banner
        with open(os.path.join(HERE, "static", "samples", "paisaleaf-banner-fixed.png"), "rb") as fh:
            new_img = fh.read()
        if not alt:
            alt = "Paisaleaf banner: Earn up to 7.10% p.a. on Paisaleaf Save. Rates are subject to change. T&Cs apply."
    if new_img:
        try:
            image, mime, _ = normalise_image(new_img)
        except Exception:
            return go(f"/drafts/{draft_id}", "That image could not be read. Use PNG, JPG or WebP.", "error")
    if form.get("remove_image") == "1":
        image, mime = None, None
    if not body and not image:
        return go(f"/drafts/{draft_id}", "The draft is empty. Add text or an image.", "error")
    n = draft["current_version"] + 1
    pages = old["pages_list"] if body == (old.get("body") or "") else None
    try:
        new_version(c, draft, n, body, image, mime, alt, old.get("source_name") or "", pages)
    except Exception:
        if S.db.scalar("SELECT 1 FROM versions WHERE draft_id = ? AND n = ? AND workspace_id = ?", (draft_id, n, c.ws)):
            return go(f"/drafts/{draft_id}", f"Already re-checked: this is version {n}.")
        raise
    S.db.audit("version.created", c.name, c.role, draft_id, {"version": n, "new_image": bool(new_img)})
    return go(f"/drafts/{draft_id}")


async def retry_check(request: Request):
    c = need_role(request, "creator", "brand")
    draft = get_draft(c.ws, int(request.path_params["id"]))
    n = draft["current_version"]
    S.db.execute("UPDATE versions SET state = 'checking', error = NULL WHERE draft_id = ? AND n = ? AND workspace_id = ? "
                 "AND state = 'failed'", (draft["id"], n, c.ws))
    queue_check(c.ws, draft["id"], n)
    return go(f"/drafts/{draft['id']}")


def routing_decision(version: dict, decisions: dict[str, dict]) -> tuple[bool, list[str]]:
    reasons = []
    findings = version["findings_list"]
    if any(f["dimension"] == "claims" for f in findings):
        reasons.append("it makes factual or promotional claims")
    overridden = [f for f in findings if decisions.get(f["key"], {}).get("action") == "reject"
                  and f["severity"] in ("blocker", "major")]
    if overridden:
        reasons.append(f"{len(overridden)} serious finding(s) were rejected by the creator")
    if any(f.get("needs_human") for f in findings):
        reasons.append("the AI was unsure about something, or a second AI opinion disagreed, so a person should judge")
    open_major = [f for f in findings if f["severity"] == "major" and f["key"] not in decisions]
    if open_major:
        reasons.append(f"{len(open_major)} major finding(s) are still open")
    return bool(reasons), reasons


async def submit(request: Request):
    c = need_role(request, "creator", "brand")
    draft_id = int(request.path_params["id"])
    draft = get_draft(c.ws, draft_id)
    if draft["status"] not in EDITABLE:
        return go(f"/drafts/{draft_id}", "This draft has already been submitted.", "error")
    if draft["working_dirty"]:
        return go(f"/drafts/{draft_id}", "You have unchecked changes. Re-check before submitting.", "error")
    version = _current_done(c, draft)
    decisions = decisions_for(c.ws, draft_id, draft["current_version"])
    blockers = [f for f in version["findings_list"] if f["severity"] == "blocker" and f["key"] not in decisions]
    if blockers:
        return go(f"/drafts/{draft_id}", f"Resolve {len(blockers)} blocker(s) first: accept a fix and re-check, "
                  "or reject with a reason.", "error")
    needs, reasons = routing_decision(version, decisions)
    if needs:
        reason_text = "; ".join(reasons)
        S.db.execute("UPDATE drafts SET status = 'in_review', review_reason = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
                     (reason_text, now_iso(), draft_id, c.ws))
        S.db.audit("draft.submitted", c.name, c.role, draft_id, {"version": draft["current_version"], "routed_because": reasons})
        return go(f"/drafts/{draft_id}", f"Sent to a reviewer because {reason_text}.")
    S.db.execute("UPDATE drafts SET status = 'approved', review_reason = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
                 ("Auto-cleared: no claims, no open serious issues", now_iso(), draft_id, c.ws))
    S.db.audit("draft.auto_approved", c.name, c.role, draft_id, {
        "version": draft["current_version"], "policy": "No claims and no open blocker or major findings"})
    return go(f"/drafts/{draft_id}", "Approved automatically: no claims and no serious issues, so no review was needed.")


async def review(request: Request):
    c = need_role(request, "reviewer", "brand")
    draft_id = int(request.path_params["id"])
    draft = get_draft(c.ws, draft_id)
    if draft["status"] != "in_review":
        return go(f"/drafts/{draft_id}", "This draft is not waiting for review (someone may have just decided).", "error")
    form = await read_form(request)
    note = (form.get("comment") or "").strip()[:2000]
    decision = form.get("decision")
    if decision == "changes" and not note:
        return go(f"/drafts/{draft_id}", "Tell the creator what to change (comment required).", "error")
    if decision not in ("approve", "changes"):
        raise HTTPException(400, "Unknown decision")
    if note:
        S.db.execute("INSERT INTO comments (workspace_id, draft_id, actor, role, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (c.ws, draft_id, c.name, c.role, note, now_iso()))
    if decision == "approve":
        version = get_version(c.ws, draft_id, draft["current_version"])
        decisions = decisions_for(c.ws, draft_id, draft["current_version"])
        overrides = [{"rule": f["rule_id"], "finding": f["title"], "reason": decisions[f["key"]]["reason"]}
                     for f in version["findings_list"] if decisions.get(f["key"], {}).get("action") == "reject"]
        S.db.execute("UPDATE drafts SET status = 'approved', updated_at = ? WHERE id = ? AND workspace_id = ?",
                     (now_iso(), draft_id, c.ws))
        S.db.audit("review.approved", c.name, c.role, draft_id, {
            "version": draft["current_version"], "comment": note, "overrides_accepted": overrides,
            "ai": version["summary_obj"].get("ai", {}).get("label", ""), "kit": version.get("kit_hash")})
        return go(f"/drafts/{draft_id}", f"Approved version {draft['current_version']}. It is now locked.")
    S.db.execute("UPDATE drafts SET status = 'changes_requested', updated_at = ? WHERE id = ? AND workspace_id = ?",
                 (now_iso(), draft_id, c.ws))
    S.db.audit("review.changes_requested", c.name, c.role, draft_id, {"version": draft["current_version"], "comment": note})
    return go(f"/drafts/{draft_id}", "Changes requested. The creator can now edit and resubmit.")


async def add_comment(request: Request):
    c = need_ws(request)
    draft_id = int(request.path_params["id"])
    get_draft(c.ws, draft_id)
    form = await read_form(request)
    body = (form.get("body") or "").strip()[:2000]
    if not body:
        return go(f"/drafts/{draft_id}#comments", "Write a comment first.", "error")
    last = S.db.one("SELECT body, actor FROM comments WHERE draft_id = ? AND workspace_id = ? ORDER BY id DESC LIMIT 1",
                    (draft_id, c.ws))
    if last and last["body"] == body and last["actor"] == c.name:
        return go(f"/drafts/{draft_id}#comments", "That comment is already posted.")
    S.db.execute("INSERT INTO comments (workspace_id, draft_id, actor, role, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (c.ws, draft_id, c.name, c.role, body, now_iso()))
    S.db.audit("comment.added", c.name, c.role, draft_id, {"comment": body[:200]})
    return go(f"/drafts/{draft_id}#comments", "Comment added.")


async def approval_record(request: Request):
    from .record import build_record

    c = need_ws(request)
    draft_id = int(request.path_params["id"])
    draft = get_draft(c.ws, draft_id)
    if draft["status"] != "approved":
        return go(f"/drafts/{draft_id}", "The approval record is available once the draft is approved.", "error")
    version = get_version(c.ws, draft_id, draft["current_version"])
    decisions = decisions_for(c.ws, draft_id, draft["current_version"])
    audit = S.db.all("SELECT * FROM audit WHERE draft_id = ? AND workspace_id = ? ORDER BY id", (draft_id, c.ws))
    comments = S.db.all("SELECT * FROM comments WHERE draft_id = ? AND workspace_id = ? ORDER BY id", (draft_id, c.ws))
    pdf = build_record(c.ws_name, draft, version, decisions, audit, comments, image_bytes(version))
    S.db.audit("record.downloaded", c.name, c.role, draft_id, {"version": draft["current_version"]})
    safe = "".join(ch if ch.isalnum() else "-" for ch in draft["title"])[:40].strip("-") or "draft"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="approval-record-{draft_id}-{safe}.pdf"', "Cache-Control": "no-store"})


async def review_queue(request: Request):
    c = need_ws(request)
    drafts = S.db.all("SELECT * FROM drafts WHERE workspace_id = ? AND status = 'in_review' ORDER BY updated_at", (c.ws,))
    recent = S.db.all("SELECT * FROM drafts WHERE workspace_id = ? AND status IN ('approved', 'changes_requested') "
                      "ORDER BY updated_at DESC LIMIT 10", (c.ws,))
    return render(request, "review.html", {"drafts": drafts, "recent": recent})


async def fix_all(request: Request):
    """One click: accept every suggested text fix (in order of severity), then re-check as a new version."""
    c = need_role(request, "creator", "brand")
    draft_id = int(request.path_params["id"])
    draft = get_draft(c.ws, draft_id)
    if draft["status"] not in EDITABLE:
        return go(f"/drafts/{draft_id}", "This draft is locked while it is in review or approved.", "error")
    form = await read_form(request)
    if form.get("base") and str(form.get("base")) != str(draft["current_version"]):
        return go(f"/drafts/{draft_id}", f"Already done: this is version {draft['current_version']}.")
    version = _current_done(c, draft)
    decided = decisions_for(c.ws, draft_id, draft["current_version"])
    text, alt = draft["working_body"] or "", draft["working_alt"] or ""
    applied = skipped = 0
    order = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
    for f in sorted(version["findings_list"], key=lambda f: order.get(f["severity"], 9)):
        fix = f.get("fix") or {}
        if f["severity"] == "ok" or f["key"] in decided or fix.get("type") not in ("replace", "remove", "append", "alt"):
            continue
        if fix["type"] == "alt":
            alt, after = fix.get("text", "")[:500], fix.get("text", "")
        else:
            new_text, _ = apply_fix(text, fix, None, draft["locked_list"])
            if new_text is None:  # already changed by an earlier fix, or would remove a locked phrase
                skipped += 1
                continue
            text, after = new_text, fix.get("text", "")
        S.db.execute(
            """INSERT INTO decisions (workspace_id, draft_id, version_n, finding_key, rule_id, severity, action, reason,
               before_text, after_text, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, 'accept', '', ?, ?, ?, ?)
               ON CONFLICT (draft_id, version_n, finding_key) DO NOTHING""",
            (c.ws, draft_id, draft["current_version"], f["key"], f["rule_id"], f["severity"], f.get("quote") or f["title"],
             after, c.name, now_iso()))
        applied += 1
    if not applied:
        return go(f"/drafts/{draft_id}", "There are no text fixes left to accept. Fix image issues in the design, "
                  "or reject findings with a reason.")
    S.db.audit("finding.accept_all", c.name, c.role, draft_id, {"version": draft["current_version"], "applied": applied})
    n = draft["current_version"] + 1
    new_version(c, draft, n, text[:MAX_TEXT].strip(), image_bytes(version), version.get("image_mime"), alt,
                version.get("source_name") or "", None)
    S.db.audit("version.created", c.name, c.role, draft_id, {"version": n})
    return go(f"/drafts/{draft_id}", f"Applied {applied} fix(es) and started re-checking as version {n}."
              + (f" {skipped} overlapped with another fix." if skipped else ""))
