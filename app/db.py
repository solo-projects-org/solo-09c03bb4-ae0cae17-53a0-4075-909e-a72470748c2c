"""SQLite schema and small transaction helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS network_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_no INTEGER NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    segment_count INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS network_segments (
    revision_id INTEGER NOT NULL REFERENCES network_revisions(id),
    seg_index INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    ax REAL NOT NULL,
    ay REAL NOT NULL,
    bx REAL NOT NULL,
    by REAL NOT NULL,
    PRIMARY KEY (revision_id, segment_id)
);
CREATE INDEX IF NOT EXISTS idx_segments_revision_order
    ON network_segments(revision_id, seg_index);

CREATE TABLE IF NOT EXISTS points_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_no INTEGER NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    point_count INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS track_points (
    revision_id INTEGER NOT NULL REFERENCES points_revisions(id),
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    PRIMARY KEY (revision_id, seq)
);

CREATE TABLE IF NOT EXISTS active_sources (
    kind TEXT PRIMARY KEY CHECK (kind IN ('network', 'points')),
    revision_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network_revision_id INTEGER NOT NULL REFERENCES network_revisions(id),
    points_revision_id INTEGER NOT NULL REFERENCES points_revisions(id),
    radius REAL NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'running', 'done', 'stale')
    ),
    claim_token TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, id);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    network_revision_id INTEGER NOT NULL REFERENCES network_revisions(id),
    points_revision_id INTEGER NOT NULL REFERENCES points_revisions(id),
    radius REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid', 'review')),
    reason TEXT,
    total_distance REAL,
    path_count INTEGER,
    assignments_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidated_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_current
    ON results(network_revision_id, points_revision_id, radius, id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_RADIUS = 10.0


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    with write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('radius', ?)",
            (str(DEFAULT_RADIUS),),
        )


@contextmanager
def write_txn(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_radius(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'radius'"
    ).fetchone()
    return float(row["value"]) if row else DEFAULT_RADIUS


def get_active_revision_id(conn: sqlite3.Connection, kind: str) -> int | None:
    row = conn.execute(
        "SELECT revision_id FROM active_sources WHERE kind = ?", (kind,)
    ).fetchone()
    return row["revision_id"] if row else None


def get_revision(conn: sqlite3.Connection, kind: str, revision_id: int | None):
    if revision_id is None:
        return None
    table = "network_revisions" if kind == "network" else "points_revisions"
    return conn.execute(
        f"SELECT * FROM {table} WHERE id = ?", (revision_id,)
    ).fetchone()


def list_revisions(conn: sqlite3.Connection, kind: str, limit: int = 20):
    table = "network_revisions" if kind == "network" else "points_revisions"
    return conn.execute(
        f"SELECT * FROM {table} ORDER BY revision_no DESC LIMIT ?", (limit,)
    ).fetchall()
