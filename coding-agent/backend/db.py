"""SQLite connection and schema. One connection, explicit transactions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
  host_id              TEXT PRIMARY KEY,
  endpoint             TEXT NOT NULL,
  container_name       TEXT NOT NULL,
  port                 INTEGER NOT NULL,
  generation           INTEGER NOT NULL DEFAULT 1,
  boot_id              TEXT,
  status               TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (status IN ('unknown','healthy','suspect','replacing','down')),
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_heartbeat_at    REAL,
  updated_at           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id        TEXT PRIMARY KEY,
  idempotency_key   TEXT UNIQUE,
  title             TEXT NOT NULL,
  prompt            TEXT NOT NULL,
  status            TEXT NOT NULL
                    CHECK (status IN ('queued','running','waiting_host',
                                      'completed','failed','cancelled')),
  step              INTEGER NOT NULL DEFAULT 0,
  max_steps         INTEGER NOT NULL,
  host_id           TEXT REFERENCES hosts(host_id),
  host_generation   INTEGER,
  lease_owner       TEXT,
  lease_expires_at  REAL,
  final_answer      TEXT,
  error             TEXT,
  created_at        REAL NOT NULL,
  updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_runnable
  ON sessions(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS messages (
  session_id       TEXT NOT NULL REFERENCES sessions(session_id),
  idx              INTEGER NOT NULL,
  role             TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
  content          TEXT,
  tool_calls_json  TEXT,
  tool_call_id     TEXT,
  step             INTEGER NOT NULL,
  created_at       REAL NOT NULL,
  PRIMARY KEY (session_id, idx)
);

CREATE TABLE IF NOT EXISTS tool_calls (
  op_id            TEXT PRIMARY KEY,
  session_id       TEXT NOT NULL REFERENCES sessions(session_id),
  step             INTEGER NOT NULL,
  call_index       INTEGER NOT NULL,
  model_call_id    TEXT NOT NULL,
  tool_name        TEXT NOT NULL,
  args_json        TEXT NOT NULL,
  status           TEXT NOT NULL
                   CHECK (status IN ('pending','running','succeeded','failed')),
  attempts         INTEGER NOT NULL DEFAULT 0,
  host_id          TEXT,
  host_generation  INTEGER,
  replayed         INTEGER NOT NULL DEFAULT 0,
  result_json      TEXT,
  error            TEXT,
  created_at       REAL NOT NULL,
  started_at       REAL,
  finished_at      REAL,
  UNIQUE (session_id, step, call_index)
);
CREATE INDEX IF NOT EXISTS tool_calls_session ON tool_calls(session_id, step, call_index);

CREATE TABLE IF NOT EXISTS events (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT,
  ts            REAL NOT NULL,
  type          TEXT NOT NULL,
  op_id         TEXT,
  host_id       TEXT,
  payload_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_session_seq ON events(session_id, seq);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn
