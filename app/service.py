"""Revision, job and result state transitions.

Invariants enforced here:
* each upload is accepted as a new immutable revision and becomes active
  atomically; all results that depend on the replaced revision are invalidated
  in the same transaction;
* a worker may only publish a result if its network revision, points revision
  and radius are still the active ones at publish time;
* a job interrupted by a restart or an exception returns to ``pending``, so
  recomputation is attempted again rather than silently lost.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import db
from .matching import (
    Segment,
    TrackPoint,
    compress_segment_sequence,
    match_points,
)
from .parsing import ParsedNetwork, ParsedPoints


class SourceChanged(RuntimeError):
    """Raised when the active sources changed while a job was computing."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _next_revision_no(conn: sqlite3.Connection, kind: str) -> int:
    table = "network_revisions" if kind == "network" else "points_revisions"
    row = conn.execute(f"SELECT COALESCE(MAX(revision_no), 0) AS n FROM {table}")
    return int(row.fetchone()["n"]) + 1


def _invalidate_results_for_source(
    conn: sqlite3.Connection, kind: str, reason: str
) -> int:
    column = (
        "network_revision_id" if kind == "network" else "points_revision_id"
    )
    active = db.get_active_revision_id(conn, kind)
    if active is None:
        return 0
    cur = conn.execute(
        f"""
        UPDATE results SET invalidated_at = ?, invalidated_reason = ?
        WHERE invalidated_at IS NULL AND {column} = ?
        """,
        (db.utc_now(), reason, active),
    )
    return cur.rowcount


def _stale_pending_jobs(conn: sqlite3.Connection, kind: str, reason: str) -> None:
    column = (
        "network_revision_id" if kind == "network" else "points_revision_id"
    )
    conn.execute(
        f"""
        UPDATE jobs SET state = 'stale', finished_at = ?, error = ?
        WHERE state IN ('pending', 'running')
          AND {column} <> COALESCE(
              (SELECT revision_id FROM active_sources WHERE kind = ?), -1
          )
        """,
        (db.utc_now(), reason, kind),
    )


def accept_network_revision(
    conn: sqlite3.Connection, parsed: ParsedNetwork, filename: str,
    note: str, raw: bytes,
) -> int:
    now = db.utc_now()
    with db.write_txn(conn):
        revision_no = _next_revision_no(conn, "network")
        cur = conn.execute(
            """
            INSERT INTO network_revisions(
                revision_no, filename, content_sha256, segment_count,
                note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                revision_no,
                filename,
                sha256_bytes(raw),
                len(parsed.segments),
                note,
                now,
            ),
        )
        revision_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO network_segments(
                revision_id, seg_index, segment_id, ax, ay, bx, by
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (revision_id, idx, seg.segment_id, seg.ax, seg.ay, seg.bx, seg.by)
                for idx, seg in enumerate(parsed.segments)
            ],
        )
        _invalidate_results_for_source(conn, "network", "NETWORK_REPLACED")
        conn.execute(
            """
            INSERT INTO active_sources(kind, revision_id) VALUES ('network', ?)
            ON CONFLICT(kind) DO UPDATE SET revision_id = excluded.revision_id
            """,
            (revision_id,),
        )
        _stale_pending_jobs(conn, "network", "NETWORK_REPLACED")
        _enqueue_current_job(conn)
    return revision_id


def accept_points_revision(
    conn: sqlite3.Connection, parsed: ParsedPoints, filename: str,
    note: str, raw: bytes,
) -> int:
    now = db.utc_now()
    with db.write_txn(conn):
        revision_no = _next_revision_no(conn, "points")
        cur = conn.execute(
            """
            INSERT INTO points_revisions(
                revision_no, filename, content_sha256, point_count,
                note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                revision_no,
                filename,
                sha256_bytes(raw),
                len(parsed.points),
                note,
                now,
            ),
        )
        revision_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO track_points(revision_id, seq, ts, x, y)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (revision_id, p.seq, p.ts, p.x, p.y)
                for p in parsed.points
            ],
        )
        _invalidate_results_for_source(conn, "points", "POINTS_REPLACED")
        conn.execute(
            """
            INSERT INTO active_sources(kind, revision_id) VALUES ('points', ?)
            ON CONFLICT(kind) DO UPDATE SET revision_id = excluded.revision_id
            """,
            (revision_id,),
        )
        _stale_pending_jobs(conn, "points", "POINTS_REPLACED")
        _enqueue_current_job(conn)
    return revision_id


def set_radius(conn: sqlite3.Connection, radius: float) -> None:
    with db.write_txn(conn):
        old = db.get_radius(conn)
        if abs(old - radius) <= 1e-12:
            return
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'radius'",
            (repr(radius),),
        )
        conn.execute(
            """
            UPDATE results SET invalidated_at = ?, invalidated_reason = 'RADIUS_CHANGED'
            WHERE invalidated_at IS NULL
            """,
            (db.utc_now(),),
        )
        conn.execute(
            """
            UPDATE jobs SET state = 'stale', finished_at = ?, error = 'RADIUS_CHANGED'
            WHERE state IN ('pending', 'running') AND radius <> ?
            """,
            (db.utc_now(), radius),
        )
        _enqueue_current_job(conn)


def _enqueue_current_job(conn: sqlite3.Connection) -> int | None:
    """Ensure one pending job exists for the active sources and radius."""
    network_id = db.get_active_revision_id(conn, "network")
    points_id = db.get_active_revision_id(conn, "points")
    if network_id is None or points_id is None:
        return None
    radius = db.get_radius(conn)
    existing = conn.execute(
        """
        SELECT id FROM jobs
        WHERE network_revision_id = ? AND points_revision_id = ?
          AND radius = ? AND state IN ('pending', 'running')
        ORDER BY id LIMIT 1
        """,
        (network_id, points_id, radius),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO jobs(
            network_revision_id, points_revision_id, radius, state, created_at
        ) VALUES (?, ?, ?, 'pending', ?)
        """,
        (network_id, points_id, radius, db.utc_now()),
    )
    return int(cur.lastrowid)


def enqueue_recompute(conn: sqlite3.Connection) -> int | None:
    with db.write_txn(conn):
        return _enqueue_current_job(conn)


def claim_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    with db.write_txn(conn):
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE state = 'pending'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id LIMIT 1
            """,
            (db.utc_now(),),
        ).fetchone()
        if row is None:
            return None
        token = uuid.uuid4().hex
        conn.execute(
            """
            UPDATE jobs SET state = 'running', claim_token = ?,
                started_at = ?, error = NULL
            WHERE id = ? AND state = 'pending'
            """,
            (token, db.utc_now(), row["id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        return claimed


def reset_running_jobs(conn: sqlite3.Connection) -> int:
    """On process start, running jobs belong to a dead worker: retry them."""
    with db.write_txn(conn):
        cur = conn.execute(
            """
            UPDATE jobs SET state = 'pending', claim_token = NULL,
                error = COALESCE(error, 'RESTART_INTERRUPTED')
            WHERE state = 'running'
            """
        )
        return cur.rowcount


def fail_job(
    conn: sqlite3.Connection, job_id: int, token: str, message: str,
    attempts: int,
) -> None:
    # Keep failed work pending so a later worker or restart retries it, but
    # back off briefly to avoid a hot loop on a persistent error.
    delay_seconds = min(60, 2 ** min(attempts, 6))
    next_attempt = (
        datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    with db.write_txn(conn):
        conn.execute(
            """
            UPDATE jobs SET state = 'pending', claim_token = NULL,
                attempts = attempts + 1, error = ?, next_attempt_at = ?
            WHERE id = ? AND state = 'running' AND claim_token = ?
            """,
            (message[:2000], next_attempt, job_id, token),
        )


def mark_stale(conn: sqlite3.Connection, job_id: int, token: str, reason: str) -> None:
    with db.write_txn(conn):
        conn.execute(
            """
            UPDATE jobs SET state = 'stale', claim_token = NULL,
                error = ?, finished_at = ?
            WHERE id = ? AND state = 'running' AND claim_token = ?
            """,
            (reason, db.utc_now(), job_id, token),
        )


def _load_network(conn: sqlite3.Connection, revision_id: int) -> list[Segment]:
    rows = conn.execute(
        """
        SELECT segment_id, ax, ay, bx, by FROM network_segments
        WHERE revision_id = ? ORDER BY seg_index
        """,
        (revision_id,),
    ).fetchall()
    return [
        Segment(r["segment_id"], r["ax"], r["ay"], r["bx"], r["by"])
        for r in rows
    ]


def _load_points(conn: sqlite3.Connection, revision_id: int) -> list[TrackPoint]:
    rows = conn.execute(
        """
        SELECT seq, ts, x, y FROM track_points
        WHERE revision_id = ? ORDER BY seq
        """,
        (revision_id,),
    ).fetchall()
    return [
        TrackPoint(r["seq"], r["ts"], r["x"], r["y"]) for r in rows
    ]


def _publish(
    conn: sqlite3.Connection, job: sqlite3.Row, token: str, outcome,
    segments: list[Segment],
) -> int:
    with db.write_txn(conn):
        network_id = db.get_active_revision_id(conn, "network")
        points_id = db.get_active_revision_id(conn, "points")
        radius = db.get_radius(conn)
        if (
            network_id != job["network_revision_id"]
            or points_id != job["points_revision_id"]
            or abs(radius - job["radius"]) > 1e-12
        ):
            raise SourceChanged("来源已换版，拒绝发布旧结果")

        # A newer job for the same sources may have superseded this one while
        # it was computing. Publishing it is still correct, but avoid stacking
        # duplicate current results.
        conn.execute(
            """
            UPDATE results SET invalidated_at = ?, invalidated_reason = 'SUPERSEDED'
            WHERE invalidated_at IS NULL
              AND network_revision_id = ? AND points_revision_id = ?
              AND radius = ?
            """,
            (
                db.utc_now(),
                job["network_revision_id"],
                job["points_revision_id"],
                job["radius"],
            ),
        )
        segment_by_id = {s.segment_id: s for s in segments}
        assignments = []
        for item in outcome.assignments:
            seg = segment_by_id[item["segment_id"]]
            assignments.append(
                {
                    "point_seq": item["point_seq"],
                    "segment_id": item["segment_id"],
                    "distance": item["distance"],
                    "segment_from": [seg.ax, seg.ay],
                    "segment_to": [seg.bx, seg.by],
                }
            )
        diagnostics = dict(outcome.diagnostics or {})
        diagnostics["candidate_totals"] = outcome.candidate_totals
        cur = conn.execute(
            """
            INSERT INTO results(
                job_id, network_revision_id, points_revision_id, radius,
                status, reason, total_distance, path_count,
                assignments_json, candidates_json, diagnostics_json,
                published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job["network_revision_id"],
                job["points_revision_id"],
                job["radius"],
                outcome.status,
                outcome.reason,
                outcome.total_distance,
                outcome.path_count,
                json.dumps(assignments, ensure_ascii=False),
                json.dumps(outcome.candidates, ensure_ascii=False),
                json.dumps(diagnostics, ensure_ascii=False),
                db.utc_now(),
            ),
        )
        result_id = int(cur.lastrowid)
        conn.execute(
            """
            UPDATE jobs SET state = 'done', claim_token = NULL,
                finished_at = ?, error = NULL
            WHERE id = ? AND state = 'running' AND claim_token = ?
            """,
            (db.utc_now(), job["id"], token),
        )
        return result_id


def execute_job(conn: sqlite3.Connection, job: sqlite3.Row) -> int | None:
    """Run one claimed job. Returns the published result id, if any."""
    token = job["claim_token"]
    if not token:
        raise RuntimeError("job has no claim token")
    segments = _load_network(conn, job["network_revision_id"])
    points = _load_points(conn, job["points_revision_id"])
    try:
        outcome = match_points(segments, points, float(job["radius"]))
        return _publish(conn, job, token, outcome, segments)
    except SourceChanged:
        mark_stale(conn, job["id"], token, "SOURCE_CHANGED_DURING_RECOMPUTE")
        return None


def current_result(conn: sqlite3.Connection):
    network_id = db.get_active_revision_id(conn, "network")
    points_id = db.get_active_revision_id(conn, "points")
    if network_id is None or points_id is None:
        return None
    radius = db.get_radius(conn)
    return conn.execute(
        """
        SELECT * FROM results
        WHERE invalidated_at IS NULL
          AND network_revision_id = ? AND points_revision_id = ?
          AND radius = ?
        ORDER BY id DESC LIMIT 1
        """,
        (network_id, points_id, radius),
    ).fetchone()


def latest_job(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def result_for_job(conn: sqlite3.Connection, job_id: int):
    return conn.execute(
        """
        SELECT * FROM results
        WHERE job_id = ? AND invalidated_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()


def segment_sequence(assignments: list[dict]) -> list[str]:
    return compress_segment_sequence(
        item["segment_id"] for item in assignments
    )
