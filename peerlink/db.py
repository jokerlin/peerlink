"""SQLite storage: letters, runs, audit log, small key-value state."""
import json
import os
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS letters (
  id TEXT PRIMARY KEY,
  direction TEXT NOT NULL,          -- out: we hold it for recipient; in: received
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,               -- json
  created REAL NOT NULL,
  expires REAL NOT NULL,
  acked REAL,
  expired REAL
);
CREATE INDEX IF NOT EXISTS letters_pending ON letters(recipient, direction, acked);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  task TEXT NOT NULL,
  kind TEXT NOT NULL,
  agent TEXT,
  caller TEXT NOT NULL,
  params TEXT NOT NULL,
  state TEXT NOT NULL,
  created REAL NOT NULL,
  started REAL,
  finished REAL,
  pid INTEGER,
  run_dir TEXT,
  timeout REAL,
  dedupe_key TEXT,
  session_id TEXT,
  turn INTEGER NOT NULL DEFAULT 1,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  result TEXT,
  error TEXT,
  exit_code INTEGER
);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS runs_dedupe ON runs(dedupe_key);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

TERMINAL = {"succeeded", "failed", "blocked", "timed_out", "cancelled"}
ACTIVE = {"queued", "running"}


def new_id(prefix):
    return f"{prefix}_{time.strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.rowcount

    # --- audit -----------------------------------------------------------
    def audit(self, actor, action, **detail):
        self.x("INSERT INTO audit(ts, actor, action, detail) VALUES (?,?,?,?)",
               (time.time(), actor, action, json.dumps(detail, ensure_ascii=False)))

    # --- kv --------------------------------------------------------------
    def get(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.x("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (k, json.dumps(v)))

    # --- runs ------------------------------------------------------------
    def run(self, rid):
        r = self.one("SELECT * FROM runs WHERE id=?", (rid,))
        return _decode_run(r)

    def runs(self, where="1=1", args=(), order="created", limit=None):
        sql = f"SELECT * FROM runs WHERE {where} ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_decode_run(r) for r in self.q(sql, args)]

    def update_run(self, rid, **fields):
        if "result" in fields and not isinstance(fields["result"], (str, type(None))):
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), rid))


def _decode_run(r):
    if not r:
        return r
    r["params"] = json.loads(r["params"]) if r.get("params") else {}
    if r.get("result"):
        try:
            r["result"] = json.loads(r["result"])
        except ValueError:
            pass
    return r
