"""Brand Kit: rules (versioned, with example tests), glossary, Claim Ledger, palette, confidential terms."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .checks.ai_checks import ai_rules, ai_tone
from .checks.pipeline import run_checks
from .db import now_iso
from .kit import DIMENSIONS, load_kit
from .seed import BUILT_IN, clear_kit_rows, seed_kit_rows
from .web import S, go, need_role, need_ws, read_form, read_upload, render

KIT_TABLES = {
    "rules": ["id", "dimension", "title", "description", "severity", "method", "pattern", "fix_hint", "do_example",
              "dont_example", "source", "active"],
    "glossary": ["preferred", "avoid", "note", "rule_id"],
    "facts": ["id", "topic", "label", "statement", "value", "approved_wording", "keywords", "evidence", "owner",
              "valid_until"],
    "palette": ["name", "hex"],
    "confidential": ["term", "note"],
}
RULE_FIELDS = KIT_TABLES["rules"]
UNTESTABLE = {"R-VIS-01", "R-ACC-01", "R-ACC-02"}  # need an image, not a text example


def rules_with_tests(ws: int) -> list[dict]:
    rules = S.db.all("SELECT * FROM rules WHERE workspace_id = ? ORDER BY id", (ws,))
    tests = {}
    for v in S.db.all("SELECT rule_id, version, tests FROM rule_versions WHERE workspace_id = ? AND tests IS NOT NULL", (ws,)):
        tests[(v["rule_id"], v["version"])] = json.loads(v["tests"] or "{}")
    for r in rules:
        r["tests"] = tests.get((r["id"], r.get("version") or 1))
        if r["tests"] is None and r.get("method") == "code":  # e.g. seeded rules: run their example tests once
            try:
                r["tests"] = run_rule_tests(ws, r, use_ai=False)
                S.db.execute("UPDATE rule_versions SET tests = ? WHERE workspace_id = ? AND rule_id = ? AND version = ?",
                             (json.dumps(r["tests"]), ws, r["id"], r.get("version") or 1))
            except Exception:
                pass
        r["built_in"] = BUILT_IN.get(r["id"])
    return rules


async def kit_page(request: Request):
    c = need_ws(request)
    tab = request.query_params.get("tab", "rules")
    if tab not in KIT_TABLES:
        tab = "rules"
    kit = load_kit(S.db, c.ws)
    edit_id = request.query_params.get("edit")
    rows = {
        "rules": rules_with_tests(c.ws),
        "glossary": S.db.all("SELECT * FROM glossary WHERE workspace_id = ? ORDER BY id", (c.ws,)),
        "facts": S.db.all("SELECT * FROM facts WHERE workspace_id = ? ORDER BY id", (c.ws,)),
        "palette": S.db.all("SELECT * FROM palette WHERE workspace_id = ? ORDER BY id", (c.ws,)),
        "confidential": S.db.all("SELECT * FROM confidential WHERE workspace_id = ? ORDER BY id", (c.ws,)),
    }
    today = date.today()
    for f in rows["facts"]:
        try:
            f["days_left"] = (date.fromisoformat(f["valid_until"]) - today).days if f.get("valid_until") else None
        except ValueError:
            f["days_left"] = None
    editing = None
    if edit_id:
        editing = next((r for r in rows[tab] if str(r.get("id")) == edit_id), None)
    pending = S.db.scalar("SELECT COUNT(*) AS n FROM candidates WHERE workspace_id = ? AND status = 'pending'", (c.ws,)) \
        if c.role == "brand" else 0
    return render(request, "kit.html", {"tab": tab, "rows": rows, "editing": editing, "kit_hash": kit.hash,
                                        "fields": KIT_TABLES, "disclaimer": kit.disclaimer, "pending": pending or 0,
                                        "next_rule_id": request.query_params.get("new_id", "")})


def clean_row(table: str, form) -> dict:
    row = {k: str(form.get(k) or "").strip() for k in KIT_TABLES[table]}
    if table == "rules":
        row["id"] = row["id"].upper()
        if not re.fullmatch(r"R-[A-Z0-9-]{3,20}", row["id"] or ""):
            raise ValueError("Rule ID must look like R-TON-05.")
        if row["dimension"] not in DIMENSIONS:
            raise ValueError("Pick a valid dimension.")
        if row["severity"] not in ("blocker", "major", "minor"):
            raise ValueError("Severity must be blocker, major or minor.")
        if row["method"] not in ("code", "ai", "code+ai"):
            raise ValueError("Method must be code, ai or code+ai.")
        if row["pattern"]:
            if len(row["pattern"]) > 400:
                raise ValueError("Pattern is too long (400 characters max).")
            try:
                re.compile(row["pattern"])
            except re.error as exc:
                raise ValueError(f"Pattern is not a valid regular expression: {exc}")
        if not row["title"]:
            raise ValueError("Give the rule a title.")
        for k in ("title", "source"):
            row[k] = row[k][:200]
        for k in ("description", "do_example", "dont_example", "fix_hint"):
            row[k] = row[k][:1000]
        raw_active = form.get("active")
        if form.get("active_present"):
            row["active"] = 1 if raw_active else 0
        else:  # CSV import: missing or blank means active
            row["active"] = 0 if str(raw_active or "1").strip().lower() in ("0", "false", "no", "off") else 1
    elif table == "facts":
        row["id"] = row["id"].upper()
        if not re.fullmatch(r"F-[A-Z0-9-]{1,20}", row["id"] or ""):
            raise ValueError("Fact ID must look like F-14.")
        if not row["label"] or not row["statement"]:
            raise ValueError("A fact needs a label and a statement.")
        if row["valid_until"]:
            try:
                date.fromisoformat(row["valid_until"])
            except ValueError:
                raise ValueError("Valid until must be a date like 2027-03-31.")
        if row["value"]:
            try:
                float(row["value"].replace(",", ""))
            except ValueError:
                raise ValueError("Number value must be a number, like 7.10 (leave it empty for facts without one).")
    elif table == "palette":
        if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", row["hex"]):
            raise ValueError("Colour must be a hex code like #11734F.")
        row["hex"] = "#" + row["hex"].lstrip("#").upper()
        if not row["name"]:
            raise ValueError("Give the colour a name.")
    elif table == "glossary":
        if not row["preferred"] or not row["avoid"]:
            raise ValueError("A glossary entry needs a preferred term and words to avoid.")
        row["rule_id"] = (row["rule_id"] or "R-TRM-01").upper()
    elif table == "confidential":
        row["term"] = re.sub(r"\s+", " ", row["term"])[:80]
        if len(row["term"]) < 2:
            raise ValueError("Enter the confidential term (at least 2 characters).")
    return row


def save_rule(ws: int, row: dict, actor: str, note: str = "", source_quote: str | None = None) -> int:
    """Insert or update a rule. A real change creates a new version; code-checkable rules are tested at once."""
    old = S.db.one("SELECT * FROM rules WHERE workspace_id = ? AND id = ?", (ws, row["id"]))
    data = {k: row[k] for k in RULE_FIELDS}
    if old and all(str(old.get(k) or "") == str(data[k] or "") for k in RULE_FIELDS):
        return old.get("version") or 1
    version = (old.get("version") or 1) + 1 if old else 1
    ts = now_iso()
    sq = source_quote if source_quote is not None else (old or {}).get("source_quote")
    if old:
        S.db.execute(
            f"UPDATE rules SET {', '.join(f'{k} = ?' for k in RULE_FIELDS if k != 'id')}, source_quote = ?, version = ?, "
            "updated_at = ? WHERE workspace_id = ? AND id = ?",
            (*[data[k] for k in RULE_FIELDS if k != "id"], sq, version, ts, ws, row["id"]))
    else:
        S.db.execute(
            f"INSERT INTO rules (workspace_id, {', '.join(RULE_FIELDS)}, source_quote, version, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in RULE_FIELDS)}, ?, ?, ?)",
            (ws, *[data[k] for k in RULE_FIELDS], sq, version, ts))
    tests = run_rule_tests(ws, data, use_ai=False) if data["method"] == "code" else None
    S.db.execute("INSERT INTO rule_versions (workspace_id, rule_id, version, data, note, tests, created_by, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (ws, row["id"], version, json.dumps(data, ensure_ascii=False), note or ("Edited" if old else "Added"),
                  json.dumps(tests) if tests else None, actor, ts))
    return version


def run_rule_tests(ws: int, rule: dict, use_ai: bool) -> dict:
    """Examples run as tests: the "don't" example should break the rule and the "do" example should not."""
    rid = rule["id"]
    out = {"ran_at": now_iso(), "ai": use_ai, "cases": []}
    if rid in UNTESTABLE or rule.get("dimension") == "visual":
        out["status"] = "n/a"
        out["note"] = "Needs an image, so it is tested with the sample banner instead of text examples."
        return out
    if not (rule.get("dont_example") or "").strip():
        out["status"] = "n/a"
        out["note"] = "Add a “don't” example to test this rule."
        return out
    kit = load_kit(S.db, ws)
    kit.rules[rid] = dict(rule, active=1, workspace_id=ws)  # test the version being saved
    kit.compiled.pop(rid, None)
    off = S.ai_off
    for label, text, should_fire in (("Don't example", rule.get("dont_example"), True), ("Do example", rule.get("do_example"), False)):
        text = (text or "").strip()
        if not text or text.startswith("("):
            continue
        res = run_checks(off, kit, {"channel": "", "goal": "", "locked": []}, text, None, None, "")
        fired = any(f["rule_id"] == rid and f["severity"] != "ok" for f in res["findings"])
        how = "code"
        if not fired and use_ai and any(r["id"] == rid for r in ai_rules(kit)):
            found, info = ai_tone(S.ai, kit, text, {"channel": "", "goal": "", "locked": []}, res["findings"])
            fired = any(f["rule_id"] == rid for f in found)
            how = "AI" if info.get("ok") else "AI unavailable"
        out["cases"].append({"label": label, "text": text[:300], "expected": "breaks the rule" if should_fire else "passes",
                             "fired": fired, "pass": fired == should_fire, "how": how})
    if not out["cases"]:
        out["status"] = "n/a"
    elif rule.get("method") in ("ai", "code+ai") and not use_ai and not all(x["pass"] for x in out["cases"]):
        out["status"] = "needs-ai"
        out["note"] = "Code alone can't judge this rule. Run the tests with AI."
    else:
        out["status"] = "pass" if all(x["pass"] for x in out["cases"]) else "fail"
    return out


async def kit_save(request: Request):
    c = need_role(request, "brand")
    table = request.path_params["table"]
    if table not in KIT_TABLES:
        raise HTTPException(404, "Unknown table")
    form = await read_form(request)
    try:
        row = clean_row(table, form)
        if table == "rules":
            existing = S.db.scalar("SELECT 1 FROM rules WHERE workspace_id = ? AND id = ?", (c.ws, row["id"]))
            if form.get("is_new") and existing:
                raise ValueError(f"Rule {row['id']} already exists. Pick another ID or edit the existing rule.")
            version = save_rule(c.ws, row, c.name)
            S.db.audit("kit.rules.saved", c.name, c.role, None, {"id": row["id"], "version": version, "title": row["title"]})
            return go(f"/kit/rules/{row['id']}", f"Saved as version {version}. New checks use it straight away.")
        if table == "glossary":
            ensure_glossary_rule(c.ws, row, c.name)
        save_row(c.ws, table, row, form.get("row_id") or None)
    except (ValueError, TypeError) as exc:
        return go(f"/kit?tab={table}", str(exc), "error")
    S.db.audit(f"kit.{table}.saved", c.name, c.role, None, {"row": {k: str(v)[:80] for k, v in row.items()}})
    return go(f"/kit?tab={table}", "Saved. New checks use it straight away.")


GLOSSARY_RULE = {"id": "R-TRM-00", "dimension": "terminology", "title": "Use the approved terms from the glossary",
                 "description": "Words and names listed in the glossary are written the approved way.", "severity": "minor",
                 "method": "code", "pattern": "", "fix_hint": "", "do_example": "", "dont_example": "",
                 "source": "Glossary", "active": 1}


def ensure_glossary_rule(ws: int, row: dict, actor: str) -> None:
    """Glossary entries are checked under a rule; if the named rule doesn't exist, use a generic one."""
    if S.db.scalar("SELECT 1 FROM rules WHERE workspace_id = ? AND id = ?", (ws, row["rule_id"])):
        return
    if not S.db.scalar("SELECT 1 FROM rules WHERE workspace_id = ? AND id = 'R-TRM-00'", (ws,)):
        save_rule(ws, dict(GLOSSARY_RULE), actor, "Created for glossary entries")
    row["rule_id"] = "R-TRM-00"


def save_row(ws: int, table: str, row: dict, row_id: str | None = None) -> None:
    ts = now_iso()
    cols = KIT_TABLES[table]
    if table == "facts":
        sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        S.db.execute(
            f"INSERT INTO facts (workspace_id, {', '.join(cols)}, updated_at) VALUES (?, {', '.join('?' for _ in cols)}, ?) "
            f"ON CONFLICT (workspace_id, id) DO UPDATE SET {sets}, updated_at = excluded.updated_at",
            (ws, *[row[c] for c in cols], ts))
    elif row_id:
        try:
            rid = int(row_id)
        except ValueError:
            raise ValueError("Unknown row.")
        S.db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = ? WHERE id = ? AND workspace_id = ?",
                     (*[row[c] for c in cols], ts, rid, ws))
    else:
        S.db.execute(f"INSERT INTO {table} (workspace_id, {', '.join(cols)}, updated_at) VALUES (?, {', '.join('?' for _ in cols)}, ?)",
                     (ws, *[row[c] for c in cols], ts))


async def kit_delete(request: Request):
    c = need_role(request, "brand")
    table = request.path_params["table"]
    if table not in KIT_TABLES:
        raise HTTPException(404, "Unknown table")
    form = await read_form(request)
    row_id = str(form.get("row_id") or "")
    if table in ("rules", "facts"):
        S.db.execute(f"DELETE FROM {table} WHERE id = ? AND workspace_id = ?", (row_id, c.ws))
    else:
        try:
            S.db.execute(f"DELETE FROM {table} WHERE id = ? AND workspace_id = ?", (int(row_id), c.ws))
        except ValueError:
            return go(f"/kit?tab={table}", "Unknown row.", "error")
    S.db.audit(f"kit.{table}.deleted", c.name, c.role, None, {"id": row_id})
    return go(f"/kit?tab={table}", "Deleted." + (" Its version history is kept." if table == "rules" else ""))


async def kit_export(request: Request):
    c = need_ws(request)
    table = request.path_params["table"]
    if table not in KIT_TABLES:
        raise HTTPException(404, "Unknown table")
    rows = S.db.all(f"SELECT {', '.join(KIT_TABLES[table])} FROM {table} WHERE workspace_id = ? ORDER BY 1", (c.ws,))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=KIT_TABLES[table])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="brandproof-{table}.csv"'})


async def kit_import(request: Request):
    c = need_role(request, "brand")
    table = request.path_params["table"]
    if table not in KIT_TABLES:
        raise HTTPException(404, "Unknown table")
    form = await read_form(request)
    data = await read_upload(form.get("file"), limit=2 * 1024 * 1024)
    if not data:
        return go(f"/kit?tab={table}", "Choose a CSV file first.", "error")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return go(f"/kit?tab={table}", "The CSV must be UTF-8 encoded (in Excel: Save As → CSV UTF-8).", "error")
    reader = csv.DictReader(io.StringIO(text))
    missing = [col for col in KIT_TABLES[table] if col not in (reader.fieldnames or []) and col not in ("active",)]
    if missing:
        return go(f"/kit?tab={table}", f"CSV is missing columns: {', '.join(missing)}", "error")
    ok, errors = 0, []
    for i, raw in enumerate(reader, start=2):
        if i > 2001:
            errors.append("stopped after 2,000 rows")
            break
        try:
            row = clean_row(table, raw)
            if table == "rules":
                save_rule(c.ws, row, c.name, "Imported from CSV")
            else:
                if table == "glossary":
                    ensure_glossary_rule(c.ws, row, c.name)
                save_row(c.ws, table, row)
            ok += 1
        except (ValueError, TypeError) as exc:
            errors.append(f"line {i}: {exc}")
    S.db.audit(f"kit.{table}.imported", c.name, c.role, None, {"rows": ok, "errors": errors[:5]})
    msg = f"Imported {ok} row(s)." + (f" Skipped {len(errors)}: {'; '.join(errors[:3])}" if errors else "")
    return go(f"/kit?tab={table}", msg, "error" if errors and not ok else "ok")


async def rule_page(request: Request):
    c = need_ws(request)
    rid = request.path_params["rule_id"].upper()
    rule = S.db.one("SELECT * FROM rules WHERE workspace_id = ? AND id = ?", (c.ws, rid))
    versions = S.db.all("SELECT * FROM rule_versions WHERE workspace_id = ? AND rule_id = ? ORDER BY version DESC, id DESC",
                        (c.ws, rid))
    if not rule and not versions:
        raise HTTPException(404, "Rule not found")
    for v in versions:
        v["data_obj"] = json.loads(v["data"] or "{}")
        v["tests_obj"] = json.loads(v["tests"]) if v.get("tests") else None
    for older, newer in zip(versions[1:], versions[:-1]):
        newer["changed"] = [k for k in RULE_FIELDS if str(newer["data_obj"].get(k) or "") != str(older["data_obj"].get(k) or "")]
    usage = rule_usage(c.ws, rid)
    return render(request, "rule.html", {"rule": rule, "versions": versions, "rid": rid, "usage": usage,
                                         "built_in": BUILT_IN.get(rid)})


def rule_usage(ws: int, rid: str) -> dict:
    """How often this rule fired and what people did about it (feeds the Clarity Radar)."""
    from .insights import rule_stats
    return rule_stats(ws).get(rid, {})


async def rule_test(request: Request):
    c = need_role(request, "brand")
    rid = request.path_params["rule_id"].upper()
    rule = S.db.one("SELECT * FROM rules WHERE workspace_id = ? AND id = ?", (c.ws, rid))
    if not rule:
        raise HTTPException(404, "Rule not found")
    form = await read_form(request)
    use_ai = form.get("ai") == "1" and S.ai.enabled
    tests = run_rule_tests(c.ws, rule, use_ai=use_ai)
    S.db.execute("UPDATE rule_versions SET tests = ? WHERE workspace_id = ? AND rule_id = ? AND version = ?",
                 (json.dumps(tests), c.ws, rid, rule.get("version") or 1))
    S.db.audit("rule.tested", c.name, c.role, None, {"id": rid, "status": tests.get("status")})
    labels = {"pass": "Tests passed.", "fail": "A test failed: see below.", "n/a": "Nothing to test for this rule.",
              "needs-ai": "Code alone can't judge this rule; run the tests with AI."}
    return go(f"/kit/rules/{rid}", labels.get(tests.get("status"), "Tests ran."), "error" if tests.get("status") == "fail" else "ok")


async def rule_restore(request: Request):
    c = need_role(request, "brand")
    rid = request.path_params["rule_id"].upper()
    form = await read_form(request)
    try:
        n = int(form.get("version") or 0)
    except ValueError:
        n = 0
    v = S.db.one("SELECT * FROM rule_versions WHERE workspace_id = ? AND rule_id = ? AND version = ? ORDER BY id DESC",
                 (c.ws, rid, n))
    if not v:
        return go(f"/kit/rules/{rid}", "That version doesn't exist.", "error")
    data = json.loads(v["data"])
    data["id"] = rid
    version = save_rule(c.ws, {k: data.get(k, "") for k in RULE_FIELDS}, c.name, f"Restored from version {n}")
    S.db.audit("kit.rules.saved", c.name, c.role, None, {"id": rid, "version": version, "restored_from": n})
    return go(f"/kit/rules/{rid}", f"Restored version {n} as version {version}.")


async def restore_demo(request: Request):
    c = need_role(request, "brand")
    if not c.is_demo:
        raise HTTPException(403, "Only the demo workspace can be restored.")
    clear_kit_rows(S.db, c.ws)
    seed_kit_rows(S.db, c.ws, "demo", actor=c.name)
    S.db.audit("kit.restored", c.name, c.role, None, {})
    return go("/kit", "The demo Brand Kit is back to its original rules, facts and colours.")
