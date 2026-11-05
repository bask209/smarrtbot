"""SQLite (stdlib, WAL mode) behind a thin repository layer.

All statements are tiny and indexed, so calls are synchronous; a lock keeps the single
connection safe across the event loop and worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
  status TEXT NOT NULL DEFAULT 'pending', lang TEXT, approved_by INTEGER, updated_at REAL);
CREATE TABLE IF NOT EXISTS chats(
  chat_id INTEGER PRIMARY KEY, type TEXT, title TEXT, seen_at REAL);
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, role TEXT NOT NULL,
  text TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS turns_chat ON turns(chat_id, id);
CREATE TABLE IF NOT EXISTS summaries(
  chat_id INTEGER PRIMARY KEY, text TEXT NOT NULL, updated_at REAL);
CREATE TABLE IF NOT EXISTS pending_actions(
  nonce TEXT PRIMARY KEY, kind TEXT NOT NULL, requester_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
  expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id INTEGER, action TEXT NOT NULL,
  target TEXT, bytes_freed INTEGER NOT NULL DEFAULT 0, dry_run INTEGER NOT NULL DEFAULT 1,
  details TEXT);
CREATE INDEX IF NOT EXISTS audit_user ON audit_log(user_id, action, ts);
CREATE TABLE IF NOT EXISTS keys_usage(
  key_id TEXT NOT NULL, model TEXT NOT NULL, day TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,
  rate_limited INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(key_id, model, day));
CREATE TABLE IF NOT EXISTS model_state(
  name TEXT PRIMARY KEY, rank INTEGER NOT NULL DEFAULT 999, status TEXT NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
  last_ok REAL, last_error TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS history_cursor(
  service TEXT PRIMARY KEY, last_id INTEGER NOT NULL, last_date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flags(
  id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, history_id INTEGER NOT NULL,
  item_id INTEGER, episode_id INTEGER, file_id INTEGER, download_id TEXT, path TEXT,
  title TEXT, year INTEGER, season INTEGER, episode INTEGER,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
  issues TEXT, created_at REAL NOT NULL, UNIQUE(service, history_id));
CREATE TABLE IF NOT EXISTS storage_snapshots(
  day TEXT PRIMARY KEY, ts REAL NOT NULL, total INTEGER, free INTEGER,
  movies INTEGER, tv INTEGER, torrents INTEGER);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL);
"""


@dataclass(frozen=True)
class PendingAction:
    nonce: str
    kind: str
    requester_id: int
    chat_id: int
    payload: dict[str, Any]
    created_at: float
    expires_at: float
    status: str


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ---- primitives -------------------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- kv ---------------------------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )

    # ---- users and chats --------------------------------------------------------------------
    def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        self.execute(
            "INSERT INTO users(user_id,username,first_name,status,updated_at) "
            "VALUES(?,?,?,'pending',?) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, first_name=excluded.first_name",
            (user_id, username, first_name, time.time()),
        )

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE user_id=?", (user_id,))

    def user_status(self, user_id: int) -> str | None:
        row = self.user(user_id)
        return row["status"] if row else None

    def set_user_status(self, user_id: int, status: str, by: int | None) -> None:
        self.execute(
            "INSERT INTO users(user_id,status,approved_by,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, "
            "approved_by=excluded.approved_by, updated_at=excluded.updated_at",
            (user_id, status, by, time.time()),
        )

    def set_user_lang(self, user_id: int, lang: str) -> None:
        self.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))

    def users(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM users ORDER BY status, user_id")

    def touch_chat(self, chat_id: int, type_: str, title: str | None) -> None:
        self.execute(
            "INSERT INTO chats(chat_id,type,title,seen_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET type=excluded.type, title=excluded.title, "
            "seen_at=excluded.seen_at",
            (chat_id, type_, title, time.time()),
        )

    # ---- conversation memory ----------------------------------------------------------------
    def add_turn(self, chat_id: int, role: str, text: str) -> None:
        self.execute(
            "INSERT INTO turns(chat_id,role,text,ts) VALUES(?,?,?,?)",
            (chat_id, role, text, time.time()),
        )

    def turns(self, chat_id: int, limit: int) -> list[sqlite3.Row]:
        rows = self.query(
            "SELECT id, role, text FROM turns WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return list(reversed(rows))

    def count_turns(self, chat_id: int) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM turns WHERE chat_id=?", (chat_id,))
        return int(row["n"]) if row else 0

    def oldest_turns(self, chat_id: int, keep: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT id, role, text FROM turns WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM turns WHERE chat_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (chat_id, chat_id, keep),
        )

    def delete_turns_upto(self, chat_id: int, max_id: int) -> None:
        self.execute("DELETE FROM turns WHERE chat_id=? AND id<=?", (chat_id, max_id))

    def summary(self, chat_id: int) -> str:
        row = self.one("SELECT text FROM summaries WHERE chat_id=?", (chat_id,))
        return row["text"] if row else ""

    def set_summary(self, chat_id: int, text: str) -> None:
        self.execute(
            "INSERT INTO summaries(chat_id,text,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (chat_id, text, time.time()),
        )

    def clear_memory(self, chat_id: int) -> None:
        self.execute("DELETE FROM turns WHERE chat_id=?", (chat_id,))
        self.execute("DELETE FROM summaries WHERE chat_id=?", (chat_id,))

    # ---- pending actions --------------------------------------------------------------------
    def insert_pending(
        self,
        nonce: str,
        kind: str,
        requester_id: int,
        chat_id: int,
        payload: dict[str, Any],
        ttl: float,
    ) -> None:
        t = time.time()
        self.execute(
            "INSERT INTO pending_actions(nonce,kind,requester_id,chat_id,payload,created_at,"
            "expires_at,status) VALUES(?,?,?,?,?,?,?,'pending')",
            (nonce, kind, requester_id, chat_id, json.dumps(payload), t, t + ttl),
        )

    def get_pending(self, nonce: str) -> PendingAction | None:
        row = self.one("SELECT * FROM pending_actions WHERE nonce=?", (nonce,))
        if not row:
            return None
        return PendingAction(
            nonce=row["nonce"],
            kind=row["kind"],
            requester_id=row["requester_id"],
            chat_id=row["chat_id"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            status=row["status"],
        )

    def transition_pending(self, nonce: str, from_status: str, to_status: str) -> bool:
        """Atomic compare-and-set. Returns True only for the single caller that won."""
        cur = self.execute(
            "UPDATE pending_actions SET status=? WHERE nonce=? AND status=?",
            (to_status, nonce, from_status),
        )
        return cur.rowcount == 1

    def finish_pending(self, nonce: str, status: str, result: str) -> None:
        self.execute(
            "UPDATE pending_actions SET status=?, result=? WHERE nonce=?", (status, result, nonce)
        )

    def purge_pending(self, older_than: float) -> None:
        self.execute("DELETE FROM pending_actions WHERE expires_at < ?", (older_than,))

    # ---- audit ------------------------------------------------------------------------------
    def audit(
        self,
        user_id: int | None,
        action: str,
        target: str,
        bytes_freed: int,
        dry_run: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO audit_log(ts,user_id,action,target,bytes_freed,dry_run,details) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                time.time(),
                user_id,
                action,
                target,
                int(bytes_freed),
                1 if dry_run else 0,
                json.dumps(details or {}),
            ),
        )

    def count_actions(self, user_id: int, actions: Iterable[str], since: float) -> int:
        acts = list(actions)
        marks = ",".join("?" * len(acts))
        row = self.one(
            f"SELECT COUNT(*) AS n FROM audit_log WHERE user_id=? AND dry_run=0 "  # noqa: S608
            f"AND action IN ({marks}) AND ts>=?",
            (user_id, *acts, since),
        )
        return int(row["n"]) if row else 0

    def bytes_deleted_since(self, since: float) -> int:
        row = self.one(
            "SELECT COALESCE(SUM(bytes_freed),0) AS n FROM audit_log "
            "WHERE dry_run=0 AND action IN ('delete','cleanup','tv_replace') AND ts>=?",
            (since,),
        )
        return int(row["n"]) if row else 0

    def recent_audit(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    # ---- key usage --------------------------------------------------------------------------
    def record_key_usage(self, key_id: str, model: str, day: str, outcome: str) -> None:
        col = {"ok": "ok", "rate_limited": "rate_limited"}.get(outcome, "errors")
        self.execute(
            f"INSERT INTO keys_usage(key_id,model,day,requests,{col}) VALUES(?,?,?,1,1) "
            f"ON CONFLICT(key_id,model,day) DO UPDATE SET requests=requests+1, {col}={col}+1",
            (key_id, model, day),
        )

    def key_usage_today(self, day: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT key_id, SUM(requests) AS requests, SUM(ok) AS ok, "
            "SUM(rate_limited) AS rate_limited, SUM(errors) AS errors "
            "FROM keys_usage WHERE day=? GROUP BY key_id",
            (day,),
        )

    # ---- model state ------------------------------------------------------------------------
    def model_rows(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM model_state ORDER BY rank, name")

    def model_row(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM model_state WHERE name=?", (name,))

    def upsert_model(self, name: str, **fields: Any) -> None:
        existing = self.model_row(name)
        fields["updated_at"] = time.time()
        if existing is None:
            fields.setdefault("status", "untested")
            cols = ["name", *fields]
            marks = ",".join("?" * len(cols))
            self.execute(
                f"INSERT INTO model_state({','.join(cols)}) VALUES({marks})",
                (name, *fields.values()),
            )
        else:
            sets = ",".join(f"{k}=?" for k in fields)
            self.execute(
                f"UPDATE model_state SET {sets} WHERE name=?",  # noqa: S608
                (*fields.values(), name),
            )

    # ---- history cursor ---------------------------------------------------------------------
    def cursor(self, service: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM history_cursor WHERE service=?", (service,))

    def set_cursor(self, service: str, last_id: int, last_date: str) -> None:
        self.execute(
            "INSERT INTO history_cursor(service,last_id,last_date) VALUES(?,?,?) "
            "ON CONFLICT(service) DO UPDATE SET last_id=excluded.last_id, "
            "last_date=excluded.last_date",
            (service, last_id, last_date),
        )

    # ---- flags (TV-friendly check queue and results) ----------------------------------------
    def insert_flag(self, **fields: Any) -> int | None:
        fields.setdefault("status", "pending")
        fields.setdefault("created_at", time.time())
        cols = list(fields)
        marks = ",".join("?" * len(cols))
        cur = self.execute(
            f"INSERT OR IGNORE INTO flags({','.join(cols)}) VALUES({marks})",
            tuple(fields.values()),
        )
        return cur.lastrowid if cur.rowcount == 1 else None

    def due_flags(self, now: float, limit: int = 20) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM flags WHERE status='pending' AND next_at<=? ORDER BY id LIMIT ?",
            (now, limit),
        )

    def flag(self, flag_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM flags WHERE id=?", (flag_id,))

    def update_flag(self, flag_id: int, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE flags SET {sets} WHERE id=?",  # noqa: S608
            (*fields.values(), flag_id),
        )

    # ---- storage snapshots ------------------------------------------------------------------
    def put_snapshot(self, day: str, **vals: int) -> None:
        self.execute(
            "INSERT INTO storage_snapshots(day,ts,total,free,movies,tv,torrents) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET ts=excluded.ts, "
            "total=excluded.total, free=excluded.free, movies=excluded.movies, tv=excluded.tv, "
            "torrents=excluded.torrents",
            (
                day,
                time.time(),
                vals.get("total"),
                vals.get("free"),
                vals.get("movies"),
                vals.get("tv"),
                vals.get("torrents"),
            ),
        )

    def snapshot_on_or_before(self, day: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM storage_snapshots WHERE day<=? ORDER BY day DESC LIMIT 1", (day,)
        )
