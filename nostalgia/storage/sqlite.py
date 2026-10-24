import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..protocol import Error, encode, fingerprint


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def identifier() -> str:
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS context_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
 kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS context_event_user ON context_events(user_id,id);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, root_id INTEGER,
 item_type TEXT NOT NULL, data TEXT NOT NULL, signature TEXT NOT NULL,
 namespace TEXT, external_id TEXT, source_version TEXT,
 produced_at TEXT, created_at TEXT NOT NULL,
 status TEXT, processing_state TEXT NOT NULL DEFAULT 'pending', reason TEXT
);
CREATE INDEX IF NOT EXISTS record_user_id ON records(user_id, id);
CREATE INDEX IF NOT EXISTS record_root ON records(user_id, root_id, id);
CREATE INDEX IF NOT EXISTS record_source ON records(user_id, namespace, external_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS record_source_version
 ON records(user_id, namespace, external_id, source_version)
 WHERE source_version IS NOT NULL;
CREATE TABLE IF NOT EXISTS versions (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, state TEXT NOT NULL,
 base_id TEXT, cursor INTEGER NOT NULL, cutoff INTEGER NOT NULL,
 input_after INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0, index_ready INTEGER NOT NULL DEFAULT 0,
 retained INTEGER NOT NULL DEFAULT 1, view_token TEXT NOT NULL,
 created_at TEXT NOT NULL, completed_at TEXT, report TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active ON versions(user_id) WHERE state='active';
CREATE UNIQUE INDEX IF NOT EXISTS one_draft ON versions(user_id) WHERE state='draft';
CREATE TABLE IF NOT EXISTS details (
 version_id TEXT NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, item_id TEXT NOT NULL, data TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(version_id, kind, item_id)
);
CREATE TABLE IF NOT EXISTS idempotency (
 user_id TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, status INTEGER NOT NULL, data TEXT, etag TEXT,
 PRIMARY KEY(user_id, scope, key)
);
CREATE TABLE IF NOT EXISTS assets (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, content_type TEXT NOT NULL,
 data BLOB NOT NULL, created_at TEXT NOT NULL
);
"""


class Store:
    """SQLite adapter. Each operation owns a short transaction, including read snapshots."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('cursor_secret', ?)",
                (secrets.token_hex(32),),
            )
            conn.commit()
            self.secret = (
                conn.execute("SELECT value FROM metadata WHERE key='cursor_secret'")
                .fetchone()[0]
                .encode()
            )
        finally:
            conn.close()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def transaction(self, *, write=False):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mutate(self, user, scope, key, request, action):
        if not key or len(key) > 200:
            raise Error(400, "idempotency_required", "Provide Idempotency-Key (1..200 characters)")
        signature = fingerprint(request)
        with self.transaction(write=True) as conn:
            old = conn.execute(
                "SELECT * FROM idempotency WHERE user_id=? AND scope=? AND key=?",
                (user, scope, key),
            ).fetchone()
            if old:
                if old["fingerprint"] != signature:
                    raise Error(409, "idempotency_conflict", "Key already used for another request")
                return old["status"], json.loads(old["data"]), old["etag"]
            self.ensure_user(conn, user)
            status, data, etag = action(conn)
            conn.execute(
                "INSERT INTO idempotency VALUES (?,?,?,?,?,?,?)",
                (user, scope, key, signature, status, encode(data), etag),
            )
            return status, data, etag

    def ensure_user(self, conn, user):
        if self.active(conn, user) is None:
            conn.execute(
                "INSERT INTO versions "
                "(id,user_id,state,cursor,cutoff,view_token,created_at,index_ready) "
                "VALUES (?,?,'active',0,0,?,?,1)",
                (identifier(), user, "empty", now()),
            )

    @staticmethod
    def active(conn, user):
        return conn.execute(
            "SELECT * FROM versions WHERE user_id=? AND state='active'", (user,)
        ).fetchone()

    def cursor(self, value):
        payload = base64.urlsafe_b64encode(encode(value).encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def read_cursor(self, token):
        try:
            if len(token) > 4096:
                raise ValueError
            payload, signature = token.split(".", 1)
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise Error(400, "invalid_cursor", "Invalid pagination cursor") from exc


class Repository:
    """Transaction-bound record adapter; SQL stays inside this module."""

    tables = {"records", "versions", "details", "assets", "context_events"}

    def __init__(self, conn):
        self.conn = conn

    def find(self, table, **where):
        if table not in self.tables:
            raise ValueError("Invalid internal table")
        predicate = " AND ".join(f"{key}=?" for key in where) or "1"
        return [
            dict(row)
            for row in self.conn.execute(
                f"SELECT * FROM {table} WHERE {predicate}", tuple(where.values())
            )
        ]

    def one(self, table, **where):
        rows = self.find(table, **where)
        return rows[0] if rows else None

    def insert(self, table, **values):
        if table not in self.tables:
            raise ValueError("Invalid internal table")
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        return self.conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
        ).lastrowid

    def update(self, table, where, **values):
        if table not in self.tables or not where:
            raise ValueError("Invalid internal update")
        assignments = ",".join(f"{key}=?" for key in values)
        predicate = " AND ".join(f"{key}=?" for key in where)
        self.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {predicate}",
            (*values.values(), *where.values()),
        )

    def delete(self, table, **where):
        if table not in self.tables or not where:
            raise ValueError("Invalid internal delete")
        predicate = " AND ".join(f"{key}=?" for key in where)
        self.conn.execute(f"DELETE FROM {table} WHERE {predicate}", tuple(where.values()))

    def records(self, user, cutoff=None, *, after=0, root_id=None):
        predicates = "user_id=? AND id<=? AND id>?"
        values = [user, cutoff if cutoff is not None else 9_223_372_036_854_775_807, after]
        if root_id is not None:
            predicates += " AND root_id=?"
            values.append(int(root_id))
        return [
            dict(row)
            for row in self.conn.execute(
                f"SELECT * FROM records WHERE {predicates} ORDER BY id",
                values,
            )
        ]

    def pending(self, user, cursor, cutoff, limit):
        where = "user_id=? AND id>? AND id<=?"
        values = (user, cursor, cutoff)
        count = self.conn.execute(
            f"SELECT COUNT(*) FROM records WHERE {where}",
            values,
        ).fetchone()[0]
        recent = [
            dict(row)
            for row in self.conn.execute(
                f"SELECT * FROM records WHERE {where} ORDER BY id DESC LIMIT ?",
                (*values, limit),
            )
        ]
        return count, list(reversed(recent))

    def events(self, user, after, limit):
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM context_events WHERE user_id=? AND id>? ORDER BY id LIMIT ?",
                (user, after, limit),
            )
        ]

    def head(self, user):
        return self.conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM records WHERE user_id=?", (user,)
        ).fetchone()[0]
