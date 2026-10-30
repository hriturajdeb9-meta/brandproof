"""Tiny database layer.

Uses PostgreSQL when DATABASE_URL is set (for example a Supabase project) and falls back to a local SQLite
file otherwise. All SQL in the app is written once with `?` placeholders and runs on both engines.

Workspaces and roles
--------------------
Every brand's data lives in a workspace. Each request runs "as" one signed-in user in one workspace (set with
`Database.acting_as`). On PostgreSQL the app then switches to the restricted `bp_app` database role and the
row-level-security policies in app/rls.sql decide what that user may read and change, from their membership
row. So even a bug in the app cannot show one workspace's drafts to another, let a reviewer edit the Brand
Kit, or rewrite the audit log. On SQLite (local development) the same rules are enforced by the app only.
"""
from __future__ import annotations

import contextvars
import copy
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger("brandproof.db")
_SQL_LOG = os.environ.get("BP_SQL_LOG", "")
_READ_CACHE_SECONDS = float(os.environ.get("BP_READ_CACHE_SECONDS", "600"))  # 0 turns the read cache off
SCHEMA_VERSION = "3"

# (user_id, workspace_id) of whoever the current request or background job acts for; None = system
_acting: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar("bp_acting", default=None)

WS = "workspace_id INTEGER NOT NULL"
# Round 2 tables whose shape changed (ai_calls and ai_cache are unchanged and kept as they are)
ROUND2_TABLES = ("meta", "rules", "glossary", "facts", "palette", "drafts", "versions", "decisions", "comments", "audit")
ROUND2_INDEXES = ("idx_versions_draft", "idx_decisions_draft", "idx_audit_draft", "idx_comments_draft")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id {pk},
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        auth TEXT NOT NULL,
        auth_id TEXT,
        password_hash TEXT,
        last_workspace INTEGER,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS workspaces (
        id {pk},
        name TEXT NOT NULL,
        is_demo INTEGER DEFAULT 0,
        disclaimer TEXT,
        created_by INTEGER,
        created_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS members (
        id {{pk}},
        {WS},
        user_id INTEGER,
        email TEXT NOT NULL,
        role TEXT NOT NULL,
        title TEXT,
        added_by TEXT,
        created_at TEXT,
        UNIQUE (workspace_id, email)
    )""",
    f"""CREATE TABLE IF NOT EXISTS rules (
        {WS},
        id TEXT NOT NULL,
        dimension TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        severity TEXT NOT NULL,
        method TEXT NOT NULL,
        pattern TEXT,
        fix_hint TEXT,
        do_example TEXT,
        dont_example TEXT,
        source TEXT,
        source_quote TEXT,
        active INTEGER DEFAULT 1,
        version INTEGER DEFAULT 1,
        updated_at TEXT,
        PRIMARY KEY (workspace_id, id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS rule_versions (
        id {{pk}},
        {WS},
        rule_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        data TEXT NOT NULL,
        note TEXT,
        tests TEXT,
        created_by TEXT,
        created_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS glossary (
        id {{pk}},
        {WS},
        preferred TEXT NOT NULL,
        avoid TEXT NOT NULL,
        note TEXT,
        rule_id TEXT,
        updated_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS facts (
        {WS},
        id TEXT NOT NULL,
        topic TEXT,
        label TEXT NOT NULL,
        statement TEXT NOT NULL,
        value TEXT,
        approved_wording TEXT,
        keywords TEXT,
        evidence TEXT,
        owner TEXT,
        valid_until TEXT,
        updated_at TEXT,
        PRIMARY KEY (workspace_id, id)
    )""",
    f"""CREATE TABLE IF NOT EXISTS palette (
        id {{pk}},
        {WS},
        name TEXT NOT NULL,
        hex TEXT NOT NULL,
        updated_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS confidential (
        id {{pk}},
        {WS},
        term TEXT NOT NULL,
        note TEXT,
        updated_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS drafts (
        id {{pk}},
        {WS},
        title TEXT NOT NULL,
        channel TEXT,
        goal TEXT,
        locked TEXT,
        status TEXT NOT NULL,
        created_by TEXT,
        created_at TEXT,
        updated_at TEXT,
        current_version INTEGER DEFAULT 1,
        working_body TEXT,
        working_alt TEXT,
        working_dirty INTEGER DEFAULT 0,
        review_reason TEXT,
        client_token TEXT,
        UNIQUE (workspace_id, client_token)
    )""",
    f"""CREATE TABLE IF NOT EXISTS versions (
        id {{pk}},
        {WS},
        draft_id INTEGER NOT NULL,
        n INTEGER NOT NULL,
        body TEXT,
        image TEXT,
        image_mime TEXT,
        alt_text TEXT,
        source_name TEXT,
        pages TEXT,
        state TEXT DEFAULT 'done',
        error TEXT,
        findings TEXT,
        summary TEXT,
        kit_hash TEXT,
        created_by TEXT,
        created_uid INTEGER,
        created_at TEXT,
        UNIQUE (draft_id, n)
    )""",
    f"""CREATE TABLE IF NOT EXISTS decisions (
        id {{pk}},
        {WS},
        draft_id INTEGER NOT NULL,
        version_n INTEGER NOT NULL,
        finding_key TEXT NOT NULL,
        rule_id TEXT,
        severity TEXT,
        action TEXT NOT NULL,
        reason TEXT,
        before_text TEXT,
        after_text TEXT,
        actor TEXT,
        created_at TEXT,
        UNIQUE (draft_id, version_n, finding_key)
    )""",
    f"""CREATE TABLE IF NOT EXISTS comments (
        id {{pk}},
        {WS},
        draft_id INTEGER NOT NULL,
        actor TEXT,
        role TEXT,
        body TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS audit (
        id {pk},
        workspace_id INTEGER,
        draft_id INTEGER,
        actor TEXT,
        role TEXT,
        action TEXT NOT NULL,
        detail TEXT,
        created_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS guidelines (
        id {{pk}},
        {WS},
        name TEXT NOT NULL,
        pages TEXT,
        page_count INTEGER,
        state TEXT DEFAULT 'reading',
        error TEXT,
        stats TEXT,
        created_by TEXT,
        created_uid INTEGER,
        created_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS candidates (
        id {{pk}},
        {WS},
        guideline_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        data TEXT NOT NULL,
        page INTEGER,
        quote TEXT,
        verified INTEGER DEFAULT 0,
        verify_note TEXT,
        status TEXT DEFAULT 'pending',
        result_id TEXT,
        decided_by TEXT,
        decided_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ai_calls (
        id {pk},
        purpose TEXT,
        provider TEXT,
        model TEXT,
        ok INTEGER,
        cached INTEGER,
        ms INTEGER,
        error TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ai_cache (
        key TEXT PRIMARY KEY,
        response TEXT,
        created_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_members_user ON members (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_drafts_ws ON drafts (workspace_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_versions_draft ON versions (draft_id, n)",
    "CREATE INDEX IF NOT EXISTS idx_versions_state ON versions (state)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_draft ON decisions (draft_id, version_n)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ws ON audit (workspace_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_draft ON audit (draft_id)",
    "CREATE INDEX IF NOT EXISTS idx_comments_draft ON comments (draft_id)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_g ON candidates (guideline_id)",
    "CREATE INDEX IF NOT EXISTS idx_rule_versions ON rule_versions (workspace_id, rule_id, version)",
]

WORKSPACE_TABLES = ["members", "rules", "rule_versions", "glossary", "facts", "palette", "confidential", "drafts",
                    "versions", "decisions", "comments", "audit", "guidelines", "candidates"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, url: str | None = None, sqlite_path: str | None = None):
        self.url = (url if url is not None else os.environ.get("DATABASE_URL", "")).strip()
        self.sqlite_path = sqlite_path or os.environ.get("SQLITE_PATH", "data/brandproof-v3.db")
        self.lock = threading.RLock()
        self.kind = "sqlite"
        self.warning = ""
        self.rls = False          # True when PostgreSQL row-level security is active
        self.rls_note = "Local SQLite database: roles are enforced by the app."
        self._conn = None
        self._state: tuple | None = None  # what the connection is currently set to act as
        self._rcache: dict = {}           # identical SELECT -> rows, emptied by any write
        if self.url.startswith(("postgres://", "postgresql://")):
            try:
                self._connect_pg()
                self.kind = "postgres"
            except Exception as exc:  # never let a bad URL take the demo down
                log.exception("PostgreSQL connection failed, falling back to SQLite")
                self.warning = f"PostgreSQL unavailable ({type(exc).__name__}); using temporary SQLite storage."
                self._connect_sqlite()
        else:
            self._connect_sqlite()

    # ----------------------------------------------------------- connections
    def _connect_pg(self):
        import psycopg  # imported lazily so local SQLite runs need no driver
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(
            self.url, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=10
        )
        self._state = None

    def _connect_sqlite(self):
        if self.sqlite_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.sqlite_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.sqlite_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.kind = "sqlite"

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    def _sql(self, sql: str) -> str:
        if self.kind == "postgres":
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    # ------------------------------------------------------------- acting as
    @staticmethod
    def acting() -> tuple[int, int] | None:
        return _acting.get()

    @contextmanager
    def acting_as(self, user_id: int | None, workspace_id: int | None):
        token = _acting.set((int(user_id), int(workspace_id)) if user_id and workspace_id else None)
        try:
            yield
        finally:
            _acting.reset(token)

    @contextmanager
    def as_system(self):
        token = _acting.set(None)
        try:
            yield
        finally:
            _acting.reset(token)

    def set_acting(self, user_id: int | None, workspace_id: int | None):
        """Set the acting user for the rest of the current context (used by the request middleware)."""
        return _acting.set((int(user_id), int(workspace_id)) if user_id and workspace_id else None)

    def _apply_state(self, cur) -> None:
        """Make the PostgreSQL session act as the current user (restricted role + settings) or as the system."""
        if not self.rls:
            return
        want = _acting.get()
        if want == self._state:
            return
        # one round trip each way (the database may be far away); the ids are integers, so inlining them is safe
        if want is None:
            cur.execute("RESET ROLE; SELECT set_config('app.user_id', '', false), set_config('app.workspace_id', '', false)")
        else:
            uid, ws = int(want[0]), int(want[1])
            cur.execute(f"SELECT set_config('app.user_id', '{uid}', false), set_config('app.workspace_id', '{ws}', false); "
                        "SET ROLE bp_app")
        self._state = want

    # ------------------------------------------------------------ read cache
    # The database can be a continent away from the server, so each statement costs a network round trip.
    # Identical SELECTs are answered from memory until the next write. This is safe because this process is
    # the only writer (one web service), every write goes through this object and empties the cache, and the
    # key includes who the query runs as, so row-level security sees exactly what it would have seen.
    def _cache_get(self, key):
        hit = self._rcache.get(key)
        if hit is None or time.monotonic() - hit[0] > _READ_CACHE_SECONDS:
            return None
        return copy.deepcopy(hit[1])

    def _cache_put(self, key, rows) -> None:
        if len(self._rcache) > 3000:
            self._rcache.clear()
        self._rcache[key] = (time.monotonic(), copy.deepcopy(rows))

    def clear_cache(self) -> None:
        with self.lock:
            self._rcache.clear()

    def _run(self, sql: str, params=(), fetch: str = "all"):
        if _SQL_LOG:  # development aid: record every statement (used to verify PostgreSQL compatibility)
            with open(_SQL_LOG, "a", encoding="utf-8") as fh:
                fh.write(" ".join(sql.split()) + "\n")
        is_read = fetch == "all" and sql.lstrip()[:6].upper() == "SELECT"
        key = (_acting.get(), sql, tuple(params)) if is_read and _READ_CACHE_SECONDS > 0 else None
        with self.lock:
            if key is not None:
                rows = self._cache_get(key)
                if rows is not None:
                    return rows
            elif not is_read:
                self._rcache.clear()  # any write may change any answer
            for attempt in (1, 2):
                try:
                    cur = self._conn.cursor()
                    self._apply_state(cur)
                    cur.execute(self._sql(sql), tuple(params))
                    if fetch == "none":
                        return None
                    rows = cur.fetchall() if cur.description else []
                    rows = [dict(r) for r in rows]
                    if key is not None:
                        self._cache_put(key, rows)
                    return rows
                except Exception as exc:
                    if self.kind == "postgres" and attempt == 1 and _is_connection_error(exc):
                        log.warning("PostgreSQL connection lost, reconnecting")
                        self._connect_pg()
                        continue
                    raise

    # ---------------------------------------------------------------- public
    def all(self, sql: str, params=()) -> list[dict]:
        return self._run(sql, params, "all")

    def one(self, sql: str, params=()) -> dict | None:
        rows = self._run(sql, params, "all")
        return rows[0] if rows else None

    def scalar(self, sql: str, params=()):
        row = self.one(sql, params)
        return None if row is None else next(iter(row.values()))

    def execute(self, sql: str, params=()) -> None:
        self._run(sql, params, "none")

    def insert(self, sql: str, params=()) -> int | None:
        """Run an INSERT ... RETURNING id and return the new id."""
        row = self.one(sql, params)
        return None if row is None else row.get("id")

    def init_schema(self) -> None:
        pk = "SERIAL PRIMARY KEY" if self.kind == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        with self.as_system():
            self._retire_round2_tables()
            for stmt in SCHEMA:
                self.execute(stmt.replace("{pk}", pk))
            if self.kind == "postgres":
                self._setup_rls()

    def _columns(self, table: str) -> set[str] | None:
        """Column names of a table, or None if it doesn't exist."""
        with self.lock:
            try:
                cur = self._conn.cursor()
                cur.execute(f"SELECT * FROM {table} LIMIT 0")
                return {d[0] for d in cur.description or []}
            except Exception:
                return None

    def _retire_round2_tables(self) -> None:
        """A Round 2 database (no workspaces) can't be used by this schema, and CREATE TABLE IF NOT EXISTS would
        leave its tables in place, so startup failed with "no such column: workspace_id". Rename those tables to
        r2_<name> (nothing is deleted) so fresh ones are created and the demo workspace is seeded again."""
        cols = self._columns("drafts")
        if cols is None or "workspace_id" in cols:
            return
        for index in ROUND2_INDEXES:  # index names are global, so the new tables couldn't reuse them
            self.execute(f"DROP INDEX IF EXISTS {index}")
        for table in ROUND2_TABLES:
            if self._columns(table) is not None:
                if self._columns(f"r2_{table}") is not None:
                    raise RuntimeError(f"Both {table} and r2_{table} exist; remove one before starting Brandproof.")
                self.execute(f"ALTER TABLE {table} RENAME TO r2_{table}")
        log.warning("Found a Round 2 database: its tables were renamed to r2_* and a new schema was created.")
        self.warning = self.warning or ("This database came from Round 2. Its tables were kept as r2_* and the demo "
                                        "workspace was created again.")

    def _setup_rls(self) -> None:
        """Install the restricted role and row-level-security policies (idempotent)."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rls.sql")
        with open(path, encoding="utf-8") as fh:
            script = fh.read()
        try:
            with self.lock:
                cur = self._conn.cursor()
                cur.execute("RESET ROLE")
                self._state = None
                cur.execute(script)
            self.rls = True
            self.rls_note = ("PostgreSQL row-level security is on: the database itself checks every read and write "
                             "against the user's workspace membership and role.")
        except Exception as exc:
            log.exception("Could not enable row-level security")
            self.rls = False
            self.rls_note = f"Row-level security could not be enabled ({type(exc).__name__}); roles are enforced by the app."
            self.warning = self.warning or "Database-level role checks are off; the app still enforces roles."

    def claim_once(self, key: str) -> bool:
        """Atomically mark a one-time job (like seeding) as done. True only for the first caller."""
        with self.as_system():
            row = self.one(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING RETURNING key",
                (key, now_iso()),
            )
        return row is not None

    def get_meta(self, key: str) -> str | None:
        with self.as_system():
            return self.scalar("SELECT value FROM meta WHERE key = ?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        with self.as_system():
            self.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def audit(self, action: str, actor: str, role: str, draft_id: int | None = None, detail: dict | None = None,
              workspace_id: int | None = None):
        ws = workspace_id if workspace_id is not None else (_acting.get() or (None, None))[1]
        self.execute(
            "INSERT INTO audit (workspace_id, draft_id, actor, role, action, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ws, draft_id, actor, role, action, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
        )


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in {"OperationalError", "InterfaceError"}
