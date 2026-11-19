"""Tests for revision replacement, invalidation and job state management."""

import csv
import io
import json

import pytest

from app import db, service
from app.matching import Segment, TrackPoint
from app.parsing import parse_network, parse_points

from conftest import run_job


def network_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["路段号", "起点x", "起点y", "终点x", "终点y"])
    writer.writerows(rows)
    return out.getvalue()


def points_csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["时刻", "x", "y"])
    writer.writerows(rows)
    return out.getvalue()


STRAIGHT_NETWORK = [
    ["S1", 0, 0, 100, 0],
    ["S2", 100, 0, 200, 0],
]


def upload(conn, kind, text, name="data.csv", note=""):
    raw = text.encode("utf-8")
    if kind == "network":
        parsed = parse_network(text)
        return service.accept_network_revision(conn, parsed, name, note, raw)
    parsed = parse_points(text)
    return service.accept_points_revision(conn, parsed, name, note, raw)


def test_accept_network_creates_incrementing_revisions(conn):
    first = upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    second = upload(conn, "network", network_csv(STRAIGHT_NETWORK + [["S3", 200, 0, 300, 0]]))
    rows = conn.execute(
        "SELECT revision_no, segment_count FROM network_revisions ORDER BY id"
    ).fetchall()
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert [r["segment_count"] for r in rows] == [2, 3]
    assert db.get_active_revision_id(conn, "network") == second != first


def test_uploading_points_enqueues_single_pending_job(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 0]]))
    upload(conn, "points", points_csv([["2026-11-18T00:00:10Z", 50, 0]]))
    jobs = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    # The superseded points revision's job is retired; exactly one pending job
    # remains for the newest pair of active sources.
    assert [j["state"] for j in jobs] == ["stale", "pending"]


def test_publish_then_network_replacement_invalidates_result(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 1],
                                      ["2026-11-18T00:00:10Z", 150, 1]]))
    job, result_id = run_job(conn)
    result = service.current_result(conn)
    assert result is not None and result["status"] == "valid"
    assert result["invalidated_at"] is None
    segs = json.loads(result["assignments_json"])
    assert [s["segment_id"] for s in segs] == ["S1", "S2"]

    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    old = conn.execute("SELECT * FROM results WHERE id = ?", (result_id,)).fetchone()
    assert old["invalidated_at"] is not None
    assert old["invalidated_reason"] == "NETWORK_REPLACED"
    assert service.current_result(conn) is None
    pending = conn.execute(
        "SELECT * FROM jobs WHERE state = 'pending'"
    ).fetchall()
    assert len(pending) == 1


def test_points_replacement_invalidates_result(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 0]]))
    run_job(conn)
    assert service.current_result(conn) is not None
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 20, 0]]))
    stale = conn.execute(
        "SELECT * FROM results WHERE invalidated_reason = 'POINTS_REPLACED'"
    ).fetchall()
    assert len(stale) == 1
    assert service.current_result(conn) is None


def test_radius_change_invalidates_and_requeues(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 9]]))
    run_job(conn)
    assert service.current_result(conn)["status"] == "valid"

    service.set_radius(conn, 5.0)
    assert service.current_result(conn) is None
    stale = conn.execute(
        "SELECT * FROM results WHERE invalidated_reason = 'RADIUS_CHANGED'"
    ).fetchall()
    assert len(stale) == 1
    # 9 m from the road, 5 m radius -> review result after recompute.
    _, result_id = run_job(conn)
    fresh = conn.execute("SELECT * FROM results WHERE id = ?", (result_id,)).fetchone()
    assert fresh["status"] == "review"
    assert fresh["reason"] == "NO_CANDIDATE"
    assert fresh["radius"] == pytest.approx(5.0)


def test_stale_job_cannot_publish_after_source_change(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 0]]))
    job = service.claim_job(conn)
    assert job is not None

    # A new network revision arrives while the job is running.
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))

    result_id = service.execute_job(conn, job)
    assert result_id is None
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["state"] == "stale"
    # The upload already retired the running job; the publish guard refused it.
    assert row["error"] == "NETWORK_REPLACED"
    assert service.current_result(conn) is None
    # The replacement revision still has its own pending job.
    pending = conn.execute(
        "SELECT * FROM jobs WHERE state = 'pending'"
    ).fetchall()
    assert len(pending) == 1


def test_radius_change_during_compute_blocks_publish(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 0]]))
    job = service.claim_job(conn)
    service.set_radius(conn, 20.0)
    assert service.execute_job(conn, job) is None
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["state"] == "stale"
    assert service.current_result(conn) is None


def test_restart_resets_running_job_to_pending(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 0]]))
    job = service.claim_job(conn)
    assert job["state"] == "running"
    assert service.reset_running_jobs(conn) == 1
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["state"] == "pending"
    assert row["claim_token"] is None
    # And it can be picked up again.
    again = service.claim_job(conn)
    assert again["id"] == job["id"]
    assert service.execute_job(conn, again) is not None


def test_failure_returns_job_to_pending_with_backoff(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 0]]))
    job = service.claim_job(conn)
    service.fail_job(conn, job["id"], job["claim_token"], "boom", 0)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    assert row["state"] == "pending"
    assert row["error"] == "boom"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    # Backoff means it is not claimable immediately.
    assert service.claim_job(conn) is None


def test_recompute_reuses_pending_job(conn):
    upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 10, 0]]))
    first = service.enqueue_recompute(conn)
    second = service.enqueue_recompute(conn)
    assert first == second


def test_old_network_segments_remain_for_audit(conn):
    first = upload(conn, "network", network_csv(STRAIGHT_NETWORK))
    upload(conn, "network", network_csv([["NEW", 0, 0, 50, 0]]))
    rows = conn.execute(
        "SELECT * FROM network_segments WHERE revision_id = ?", (first,)
    ).fetchall()
    assert len(rows) == 2


def test_parse_points_normalises_offsets_and_checks_order():
    parsed = parse_points(
        points_csv(
            [
                ["2026-11-18T08:00:00+08:00", 1, 2],
                ["2026-11-18T00:00:05Z", 3, 4],
            ]
        )
    )
    assert parsed.points[0].ts == "2026-11-18T00:00:00Z"
    assert parsed.points[1].ts == "2026-11-18T00:00:05Z"

    with pytest.raises(Exception) as excinfo:
        parse_points(
            points_csv(
                [
                    ["2026-11-18T00:00:10Z", 1, 2],
                    ["2026-11-18T00:00:05Z", 3, 4],
                ]
            )
        )
    assert "升序" in str(excinfo.value)
