"""Unit tests for dwell evaluation and service-level dwell-check lifecycle."""

import csv
import io
import json

import pytest

from app import db, service
from app.dwell import (
    DwellParams,
    REASON_GAP_EXCEEDS_LIMIT,
    REASON_NO_QUALIFYING_POINTS,
    REASON_SPAN_TOO_SHORT,
    REASON_TRAJECTORY_UNDER_REVIEW,
    STATUS_CONFIRMED,
    STATUS_INSUFFICIENT,
    STATUS_UNAVAILABLE,
    evaluate_dwell,
)
from app.parsing import parse_network, parse_points

from conftest import run_job


def params(**overrides):
    base = dict(
        segment_id="S1",
        x=100.0,
        y=0.0,
        radius=15.0,
        min_dwell_seconds=60.0,
        max_gap_seconds=30.0,
    )
    base.update(overrides)
    return DwellParams(**base)


def track_point(seq, ts, x, y=1.0, segment_id="S1"):
    return {"seq": seq, "ts": ts, "x": x, "y": y, "segment_id": segment_id}


def ts(minutes, seconds):
    return f"2026-11-18T00:{minutes:02d}:{seconds:02d}Z"


# ---------------------------------------------------------------------------
# evaluate_dwell (pure logic)
# ---------------------------------------------------------------------------


def test_confirmed_when_continuous_and_long_enough():
    points = [
        track_point(1, ts(0, 0), 20),      # outside station radius
        track_point(2, ts(0, 10), 95),
        track_point(3, ts(0, 20), 100),
        track_point(4, ts(0, 30), 105),
        track_point(5, ts(0, 40), 100),
        track_point(6, ts(0, 50), 102),
        track_point(7, ts(1, 0), 104),
        track_point(8, ts(1, 10), 100),
        track_point(9, ts(1, 20), 300),    # leaves the station
    ]
    out = evaluate_dwell("valid", points, params())
    assert out.status == STATUS_CONFIRMED
    assert out.observed_start == ts(0, 10)
    assert out.observed_end == ts(1, 10)
    assert out.span_seconds == pytest.approx(60.0)
    assert [p["seq"] for p in out.evidence] == [2, 3, 4, 5, 6, 7, 8]


def test_broken_sampling_cannot_be_pieced_into_dwell():
    # Two 20-second clusters separated by a 40 s gap (> 30 s limit). Total
    # span across the gap is 80 s >= 60 s, but the gap must break the dwell.
    points = [
        track_point(1, ts(0, 0), 100),
        track_point(2, ts(0, 10), 100),
        track_point(3, ts(0, 20), 100),
        track_point(4, ts(1, 0), 100),
        track_point(5, ts(1, 10), 100),
        track_point(6, ts(1, 20), 100),
    ]
    out = evaluate_dwell("valid", points, params())
    assert out.status == STATUS_INSUFFICIENT
    assert out.reason == REASON_GAP_EXCEEDS_LIMIT
    assert out.gap_count == 1
    assert out.observed_start is None and out.observed_end is None
    assert out.evidence == []


def test_gap_exactly_at_limit_stays_continuous():
    points = [
        track_point(1, ts(0, 0), 100),
        track_point(2, ts(0, 30), 100),   # gap == max_gap: allowed
        track_point(3, ts(1, 0), 100),
    ]
    out = evaluate_dwell("valid", points, params(max_gap_seconds=30.0))
    assert out.status == STATUS_CONFIRMED
    assert out.span_seconds == pytest.approx(60.0)


def test_span_exactly_at_minimum_confirms():
    points = [
        track_point(1, ts(0, 0), 100),
        track_point(2, ts(1, 0), 100),
    ]
    out = evaluate_dwell(
        "valid", points, params(min_dwell_seconds=60.0, max_gap_seconds=60.0)
    )
    assert out.status == STATUS_CONFIRMED


def test_continuous_but_too_short_is_insufficient():
    points = [
        track_point(1, ts(0, 0), 100),
        track_point(2, ts(0, 10), 100),
        track_point(3, ts(0, 20), 100),
    ]
    out = evaluate_dwell("valid", points, params())
    assert out.status == STATUS_INSUFFICIENT
    assert out.reason == REASON_SPAN_TOO_SHORT
    assert out.diagnostics["longest_run_span_seconds"] == pytest.approx(20.0)


def test_points_on_other_segment_or_outside_radius_do_not_qualify():
    points = [
        track_point(1, ts(0, 0), 100, segment_id="S2"),   # wrong segment
        track_point(2, ts(0, 10), 130),                   # 30 m away > radius
        track_point(3, ts(0, 20), 100, segment_id=None),  # unassigned
    ]
    out = evaluate_dwell("valid", points, params())
    assert out.status == STATUS_INSUFFICIENT
    assert out.reason == REASON_NO_QUALIFYING_POINTS
    assert out.qualifying_count == 0


def test_point_exactly_on_radius_boundary_qualifies():
    points = [
        track_point(1, ts(0, 0), 115, y=0.0),   # exactly 15 m from station
        track_point(2, ts(1, 0), 100, y=0.0),
    ]
    out = evaluate_dwell(
        "valid", points, params(min_dwell_seconds=60.0, max_gap_seconds=60.0)
    )
    assert out.status == STATUS_CONFIRMED
    assert out.qualifying_count == 2


def test_review_trajectory_is_unavailable_never_confirmed():
    points = [track_point(1, ts(0, 0), 100), track_point(2, ts(1, 0), 100)]
    out = evaluate_dwell("review", points, params())
    assert out.status == STATUS_UNAVAILABLE
    assert out.reason == REASON_TRAJECTORY_UNDER_REVIEW
    assert out.evidence == []


# ---------------------------------------------------------------------------
# service-level lifecycle
# ---------------------------------------------------------------------------


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


NET = network_csv([["S1", 0, 0, 500, 0]])

DWELL_POINTS = points_csv([
    ["2026-11-18T00:00:00Z", 20, 1],
    ["2026-11-18T00:00:10Z", 95, 1],
    ["2026-11-18T00:00:20Z", 100, 1],
    ["2026-11-18T00:00:30Z", 105, 1],
    ["2026-11-18T00:00:40Z", 100, 1],
    ["2026-11-18T00:00:50Z", 102, 1],
    ["2026-11-18T00:01:00Z", 104, 1],
    ["2026-11-18T00:01:10Z", 100, 1],
    ["2026-11-18T00:01:20Z", 300, 1],
])

GAP_POINTS = points_csv([
    ["2026-11-18T00:00:00Z", 100, 1],
    ["2026-11-18T00:00:10Z", 100, 1],
    ["2026-11-18T00:00:20Z", 100, 1],
    ["2026-11-18T00:01:00Z", 100, 1],
    ["2026-11-18T00:01:10Z", 100, 1],
    ["2026-11-18T00:01:20Z", 100, 1],
])


def upload(conn, kind, text, name="data.csv", note=""):
    raw = text.encode("utf-8")
    if kind == "network":
        return service.accept_network_revision(
            conn, parse_network(text), name, note, raw
        )
    return service.accept_points_revision(
        conn, parse_points(text), name, note, raw
    )


def setup_valid_result(conn, points_text=DWELL_POINTS):
    upload(conn, "network", NET)
    upload(conn, "points", points_text)
    _, result_id = run_job(conn)
    return result_id


def make_check(conn, **overrides):
    args = dict(
        segment_id="S1", x=100.0, y=0.0, radius=15.0,
        min_dwell_seconds=60.0, max_gap_seconds=30.0,
    )
    args.update(overrides)
    return service.create_dwell_check(conn, **args)


def run_dwell_job(conn):
    job = service.claim_dwell_job(conn)
    assert job is not None, "expected a pending dwell job"
    conclusion_id = service.execute_dwell_job(conn, job)
    return job, conclusion_id


def test_check_created_pending_until_executed(conn):
    result_id = setup_valid_result(conn)
    check_id = make_check(conn)
    assert service.current_dwell_conclusion(conn, check_id) is None
    job = service.claim_dwell_job(conn)
    assert job["check_id"] == check_id
    assert job["result_id"] == result_id
    assert job["state"] == "running"


def test_confirmed_conclusion_binds_result_revisions_and_radius(conn):
    result_id = setup_valid_result(conn)
    check_id = make_check(conn)
    _, conclusion_id = run_dwell_job(conn)
    row = service.current_dwell_conclusion(conn, check_id)
    assert row["id"] == conclusion_id
    assert row["status"] == "confirmed"
    assert row["result_id"] == result_id
    result = conn.execute(
        "SELECT * FROM results WHERE id = ?", (result_id,)
    ).fetchone()
    assert row["network_revision_id"] == result["network_revision_id"]
    assert row["points_revision_id"] == result["points_revision_id"]
    assert row["match_radius"] == pytest.approx(result["radius"])
    assert row["observed_start"] == "2026-11-18T00:00:10Z"
    assert row["observed_end"] == "2026-11-18T00:01:10Z"
    evidence = json.loads(row["evidence_json"])
    assert len(evidence) == 7
    assert evidence[0]["ts"] == "2026-11-18T00:00:10Z"


def test_broken_sampling_conclusion_is_insufficient(conn):
    setup_valid_result(conn, points_text=GAP_POINTS)
    check_id = make_check(conn)
    run_dwell_job(conn)
    row = service.current_dwell_conclusion(conn, check_id)
    assert row["status"] == "insufficient"
    assert row["reason"] == "GAP_EXCEEDS_LIMIT"
    assert row["observed_start"] is None


def test_review_trajectory_yields_unavailable_conclusion(conn):
    # Two disconnected parallel roads with an exact tie -> review, no unique
    # trajectory; the check must not conclude 已驻留.
    upload(conn, "network", network_csv([["P1", 0, 0, 100, 0],
                                         ["P2", 0, 5, 100, 5]]))
    upload(conn, "points", points_csv([["2026-11-18T00:00:00Z", 30, 0],
                                       ["2026-11-18T00:00:10Z", 70, 5]]))
    _, result_id = run_job(conn)
    result = conn.execute(
        "SELECT status FROM results WHERE id = ?", (result_id,)
    ).fetchone()
    assert result["status"] == "review"
    check_id = make_check(conn, segment_id="P1")
    run_dwell_job(conn)
    row = service.current_dwell_conclusion(conn, check_id)
    assert row["status"] == "unavailable"
    assert row["reason"] == "TRAJECTORY_UNDER_REVIEW"


def test_points_replacement_revokes_conclusion_and_requeues(conn):
    setup_valid_result(conn)
    check_id = make_check(conn)
    _, conclusion_id = run_dwell_job(conn)

    upload(conn, "points", GAP_POINTS)
    old = conn.execute(
        "SELECT * FROM dwell_conclusions WHERE id = ?", (conclusion_id,)
    ).fetchone()
    assert old["invalidated_at"] is not None
    assert old["invalidated_reason"] == "POINTS_REPLACED"
    assert service.current_dwell_conclusion(conn, check_id) is None

    # The recompute publishes a new trajectory; the check is re-queued
    # against it automatically.
    _, new_result_id = run_job(conn)
    job = service.claim_dwell_job(conn)
    assert job["result_id"] == new_result_id
    new_conclusion_id = service.execute_dwell_job(conn, job)
    row = service.current_dwell_conclusion(conn, check_id)
    assert row["id"] == new_conclusion_id
    assert row["status"] == "insufficient"   # new points have a sampling gap
    assert row["result_id"] == new_result_id


def test_network_replacement_revokes_conclusion(conn):
    setup_valid_result(conn)
    check_id = make_check(conn)
    _, conclusion_id = run_dwell_job(conn)

    upload(conn, "network", network_csv([["S1", 0, 0, 500, 0],
                                         ["S2", 500, 0, 900, 0]]))
    old = conn.execute(
        "SELECT * FROM dwell_conclusions WHERE id = ?", (conclusion_id,)
    ).fetchone()
    assert old["invalidated_reason"] == "NETWORK_REPLACED"
    assert service.current_dwell_conclusion(conn, check_id) is None

    _, new_result_id = run_job(conn)
    job = service.claim_dwell_job(conn)
    assert job["result_id"] == new_result_id
    service.execute_dwell_job(conn, job)
    assert service.current_dwell_conclusion(conn, check_id)["status"] == (
        "confirmed"
    )


def test_radius_change_revokes_conclusion(conn):
    setup_valid_result(conn)
    check_id = make_check(conn)
    _, conclusion_id = run_dwell_job(conn)

    service.set_radius(conn, 25.0)
    old = conn.execute(
        "SELECT * FROM dwell_conclusions WHERE id = ?", (conclusion_id,)
    ).fetchone()
    assert old["invalidated_reason"] == "RADIUS_CHANGED"
    assert service.current_dwell_conclusion(conn, check_id) is None

    _, new_result_id = run_job(conn)
    job = service.claim_dwell_job(conn)
    assert job["result_id"] == new_result_id
    assert job["match_radius"] == pytest.approx(25.0)


def test_interleaved_source_change_blocks_dwell_publish(conn):
    """核对与匹配交错：核对任务计算期间换版，不得发布旧版结论。"""
    setup_valid_result(conn)
    check_id = make_check(conn)
    job = service.claim_dwell_job(conn)
    assert job is not None

    # Points revision arrives while the dwell job is running.
    upload(conn, "points", GAP_POINTS)

    conclusion_id = service.execute_dwell_job(conn, job)
    assert conclusion_id is None
    row = conn.execute(
        "SELECT * FROM dwell_jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    assert row["state"] == "stale"
    assert service.current_dwell_conclusion(conn, check_id) is None
    # No conclusion may reference the old points revision.
    leftovers = conn.execute(
        """
        SELECT * FROM dwell_conclusions
        WHERE invalidated_at IS NULL AND points_revision_id = ?
        """,
        (job["points_revision_id"],),
    ).fetchall()
    assert leftovers == []


def test_interleaved_radius_change_blocks_dwell_publish(conn):
    setup_valid_result(conn)
    check_id = make_check(conn)
    job = service.claim_dwell_job(conn)
    service.set_radius(conn, 25.0)
    assert service.execute_dwell_job(conn, job) is None
    row = conn.execute(
        "SELECT * FROM dwell_jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    assert row["state"] == "stale"
    assert service.current_dwell_conclusion(conn, check_id) is None


def test_restart_resets_running_dwell_job(conn):
    setup_valid_result(conn)
    make_check(conn)
    job = service.claim_dwell_job(conn)
    assert job["state"] == "running"
    assert service.reset_running_dwell_jobs(conn) == 1
    row = conn.execute(
        "SELECT * FROM dwell_jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    assert row["state"] == "pending"
    assert row["claim_token"] is None
    again = service.claim_dwell_job(conn)
    assert again["id"] == job["id"]
    assert service.execute_dwell_job(conn, again) is not None


def test_failed_dwell_job_returns_to_pending_with_backoff(conn):
    setup_valid_result(conn)
    make_check(conn)
    job = service.claim_dwell_job(conn)
    service.fail_dwell_job(conn, job["id"], job["claim_token"], "boom", 0)
    row = conn.execute(
        "SELECT * FROM dwell_jobs WHERE id = ?", (job["id"],)
    ).fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    assert service.claim_dwell_job(conn) is None  # backoff not elapsed


def test_check_without_result_stays_pending_and_recovers(conn):
    upload(conn, "network", NET)
    upload(conn, "points", DWELL_POINTS)
    # No matching result yet: the check is registered but no dwell job exists.
    check_id = make_check(conn)
    assert service.claim_dwell_job(conn) is None
    assert service.current_dwell_conclusion(conn, check_id) is None

    # Once the trajectory is published the check is queued automatically.
    run_job(conn)
    job = service.claim_dwell_job(conn)
    assert job is not None and job["check_id"] == check_id


def test_manual_recheck_enqueues_against_current_result(conn):
    result_id = setup_valid_result(conn)
    check_id = make_check(conn)
    run_dwell_job(conn)
    job_id = service.enqueue_dwell_check(conn, check_id)
    assert job_id is not None
    job = conn.execute(
        "SELECT * FROM dwell_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert job["result_id"] == result_id
    # A second recheck reuses the same pending job.
    assert service.enqueue_dwell_check(conn, check_id) == job_id


def test_second_conclusion_supersedes_first(conn):
    setup_valid_result(conn)
    check_id = make_check(conn)
    run_dwell_job(conn)
    service.enqueue_dwell_check(conn, check_id)
    run_dwell_job(conn)
    live = conn.execute(
        """
        SELECT * FROM dwell_conclusions
        WHERE check_id = ? AND invalidated_at IS NULL
        """,
        (check_id,),
    ).fetchall()
    assert len(live) == 1
    superseded = conn.execute(
        """
        SELECT * FROM dwell_conclusions
        WHERE check_id = ? AND invalidated_reason = 'SUPERSEDED'
        """,
        (check_id,),
    ).fetchall()
    assert len(superseded) == 1


def test_dwell_job_staled_by_source_change_while_pending(conn):
    setup_valid_result(conn)
    check_id = make_check(conn)
    upload(conn, "points", GAP_POINTS)
    # The pending dwell job bound to the old points revision is retired.
    rows = conn.execute(
        """
        SELECT j.* FROM dwell_jobs j
        JOIN dwell_checks c ON c.id = j.check_id
        WHERE c.id = ? AND j.state = 'stale'
        """,
        (check_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["error"] == "POINTS_REPLACED"
