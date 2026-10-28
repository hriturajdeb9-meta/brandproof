"""Tiny database layer.

Uses PostgreSQL when DATABASE_URL is set (for example a Supabase project) and
falls back to a local SQLite file otherwise. All SQL in the app is written once
with `?` placeholders and runs on both engines.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger("brandproof.db")
_SQL_LOG = os.environ.get("BP_SQL_LOG", "")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rules (
        id TEXT PRIMARY KEY,
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
        active INTEGER DEFAULT 1,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS glossary (
        id {pk},
        preferred TEXT NOT NULL,
        avoid TEXT NOT NULL,
        note TEXT,
        rule_id TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS facts (
        id TEXT PRIMARY KEY,
        topic TEXT,
        label TEXT NOT NULL,
        statement TEXT NOT NULL,
        value TEXT,
        approved_wording TEXT,
        keywords TEXT,
        evidence TEXT,
        owner TEXT,
        valid_until TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS palette (
        id {pk},
        name TEXT NOT NULL,
        hex TEXT NOT NULL,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS drafts (
        id {pk},
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
        review_reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS versions (
        id {pk},
        draft_id INTEGER NOT NULL,
        n INTEGER NOT NULL,
        body TEXT,
        image TEXT,
        image_mime TEXT,
        alt_text TEXT,
        source_name TEXT,
        findings TEXT,
        summary TEXT,
        kit_hash TEXT,
        created_by TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id {pk},
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
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS comments (
        id {pk},
        draft_id INTEGER NOT NULL,
        actor TEXT,
        role TEXT,
        body TEXT,
        created_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS audit (
        id {pk},
        draft_id INTEGER,
        actor TEXT,
        role TEXT,
        action TEXT NOT NULL,
        detail TEXT,
        created_at TEXT
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
    "CREATE INDEX IF NOT EXISTS idx_versions_draft ON versions (draft_id, n)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_draft ON decisions (draft_id, version_n)",
    "CREATE INDEX IF NOT EXISTS idx_audit_draft ON audit (draft_id)",
    "CREATE INDEX IF NOT EXISTS idx_comments_draft ON comments (draft_id)",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, url: str | None = None, sqlite_path: str | None = None):
        self.url = (url if url is not None else os.environ.get("DATABASE_URL", "")).strip()
        self.sqlite_path = sqlite_path or os.environ.get("SQLITE_PATH", "data/brandproof.db")
        self.lock = threading.RLock()
        self.kind = "sqlite"
        self.warning = ""
        self._conn = None
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

    def _connect_sqlite(self):
        if self.sqlite_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.sqlite_path)), exist_ok=True)
        self._conn = sqlite3.connect(self.sqlite_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.kind = "sqlite"

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    def _sql(self, sql: str) -> str:
        if self.kind == "postgres":
            return sql.replace("?", "%s")
        return sql

    def _run(self, sql: str, params=(), fetch: str = "all"):
        if _SQL_LOG:  # development aid: record every statement (used to verify PostgreSQL compatibility)
            with open(_SQL_LOG, "a", encoding="utf-8") as fh:
                fh.write(" ".join(sql.split()) + "\n")
        with self.lock:
            for attempt in (1, 2):
                try:
                    cur = self._conn.cursor()
                    cur.execute(self._sql(sql), tuple(params))
                    if fetch == "none":
                        return None
                    rows = cur.fetchall() if cur.description else []
                    return [dict(r) for r in rows]
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
        for stmt in SCHEMA:
            self.execute(stmt.replace("{pk}", pk))

    def claim_once(self, key: str) -> bool:
        """Atomically mark a one-time job (like seeding) as done. True only for the first caller."""
        row = self.one(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, now_iso()),
        )
        return row is not None

    def get_meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key = ?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def audit(self, action: str, actor: str, role: str, draft_id: int | None = None, detail: dict | None = None):
        self.execute(
            "INSERT INTO audit (draft_id, actor, role, action, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (draft_id, actor, role, action, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
        )


def _is_connection_error(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in {"OperationalError", "InterfaceError"}
