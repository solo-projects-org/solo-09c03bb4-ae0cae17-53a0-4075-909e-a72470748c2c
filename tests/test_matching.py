"""Unit tests for the geometric / dynamic-programming matcher."""

import pytest

from app.matching import (
    Segment,
    TrackPoint,
    match_points,
    point_segment_distance,
    STATUS_REVIEW,
    STATUS_VALID,
)


def pts(*coords):
    return [
        TrackPoint(seq=i + 1, ts=f"2026-11-18T00:00:0{i}Z", x=x, y=y)
        for i, (x, y) in enumerate(coords)
    ]


def seg(sid, ax, ay, bx, by):
    return Segment(sid, ax, ay, bx, by)


def straight_network():
    # Two segments joined end to end along the +x direction.
    return [seg("A", 0, 0, 100, 0), seg("B", 100, 0, 200, 0)]


def test_point_segment_distance_clamps_to_endpoints():
    s = seg("A", 0, 0, 10, 0)
    assert point_segment_distance(5, 3, s) == pytest.approx(3)
    assert point_segment_distance(-4, 0, s) == pytest.approx(4)
    assert point_segment_distance(14, 0, s) == pytest.approx(4)


def test_single_segment_unique_path():
    out = match_points(straight_network(), pts((10, 1), (50, 2)), 10)
    assert out.status == STATUS_VALID
    assert [a["segment_id"] for a in out.assignments] == ["A", "A"]
    assert out.total_distance == pytest.approx(3)


def test_forward_transition_into_next_segment():
    out = match_points(straight_network(), pts((90, 0), (110, 0)), 15)
    assert out.status == STATUS_VALID
    assert [a["segment_id"] for a in out.assignments] == ["A", "B"]


def test_backward_transition_is_rejected():
    network = [seg("A", 0, 0, 100, 0), seg("B", 100, 0, 200, 0)]
    # p1 can only be B and p2 can only be A: the only geometric assignment
    # would drive backwards along the one-way segments.
    out = match_points(network, pts((150, 0), (50, 0)), 30)
    assert out.status == STATUS_REVIEW
    assert out.reason == "NO_CONNECTED_PATH"
    assert out.diagnostics["point_index"] == 1


def test_jump_over_intermediate_segment_is_not_allowed():
    network = [
        seg("A", 0, 0, 100, 0),
        seg("B", 100, 0, 200, 0),
        seg("C", 200, 0, 300, 0),
    ]
    # A ends at (100, 0) which is B's start, not C's; A -> C must not be
    # treated as reachable just because both are on the same straight line.
    out = match_points(network, pts((50, 0), (250, 0)), 15)
    assert out.status == STATUS_REVIEW
    assert out.reason == "NO_CONNECTED_PATH"

    # With a point on B in between, the same two ends are connected.
    out = match_points(network, pts((50, 0), (150, 0), (250, 0)), 15)
    assert out.status == STATUS_VALID
    assert [a["segment_id"] for a in out.assignments] == ["A", "B", "C"]


def test_radius_excludes_segments():
    network = [seg("A", 0, 0, 100, 0)]
    out = match_points(network, pts((50, 0), (50, 30)), 10)
    assert out.status == STATUS_REVIEW
    assert out.reason == "NO_CANDIDATE"
    assert out.diagnostics["point_index"] == 1


def test_parallel_roads_are_resolved_by_total_distance():
    # Two parallel one-way roads: (A, A2) at y=0 and (B, B2) at y=5.
    network = [
        seg("A", 0, 0, 100, 0),
        seg("A2", 100, 0, 200, 0),
        seg("B", 0, 5, 100, 5),
        seg("B2", 100, 5, 200, 5),
    ]
    # Each point is individually closer to a different road, but only the
    # y=0 chain is both connected and has the smaller total distance.
    out = match_points(network, pts((40, 0.4), (160, 1.0)), 30)
    assert out.status == STATUS_VALID
    assert [a["segment_id"] for a in out.assignments] == ["A", "A2"]
    assert out.total_distance == pytest.approx(1.4)


def test_parallel_segments_deterministic_nearest_chain():
    network = [
        seg("A", 0, 0, 100, 0),
        seg("B", 0, 5, 100, 5),
        seg("A2", 100, 0, 200, 0),
        seg("B2", 100, 5, 200, 5),
    ]
    out = match_points(network, pts((40, 0.4), (160, 0.6)), 30)
    assert out.status == STATUS_VALID
    assert [a["segment_id"] for a in out.assignments] == ["A", "A2"]


def test_exact_tie_between_parallel_paths_is_ambiguous():
    # Two disjoint parallel roads, no shared endpoints: a -> a and b -> b both
    # have total distance 0.
    network = [seg("u", 0, 0, 100, 0), seg("v", 0, 5, 100, 5)]
    out = match_points(network, pts((30, 0), (70, 5)), 30)
    assert out.status == STATUS_REVIEW
    assert out.reason == "AMBIGUOUS"
    assert out.path_count == 2


def test_near_tie_beyond_tolerance_picks_unique():
    # 2.499 m from u and 2.501 m from v: a strict but tiny difference still
    # counts as a unique optimum.
    network = [seg("u", 0, 0, 100, 0), seg("v", 0, 5, 100, 5)]
    out = match_points(network, pts((30, 2.499)), 10)
    assert out.status == STATUS_VALID
    assert [a["segment_id"] for a in out.assignments] == ["u"]


def test_candidates_reported_for_every_point():
    out = match_points(straight_network(), pts((10, 1), (150, 1)), 30)
    assert len(out.candidates) == 2
    assert [c["segment_id"] for c in out.candidates[0]] == ["A"]
    assert [c["segment_id"] for c in out.candidates[1]] == ["B"]


def test_global_minimum_not_greedy():
    # Greedy per point would take the nearest segment at each step; here that
    # makes point 2 land on a disconnected segment with a huge detour, while a
    # slightly farther first choice yields a connected cheaper total.
    network = [
        seg("A", 0, 0, 100, 0),
        seg("B", 100, 0, 200, 0),
        seg("X", 0, 4, 100, 4),
    ]
    points = pts((50, 3.9), (150, 0))
    out = match_points(network, points, 10)
    assert out.status == STATUS_VALID
    assert [a["segment_id"] for a in out.assignments] == ["A", "B"]


def test_empty_points_is_review():
    out = match_points(straight_network(), [], 10)
    assert out.status == STATUS_REVIEW


def test_path_count_counts_all_tied_suffixes():
    # Segments u and v both reachable, then two identical-cost continuations.
    network = [
        seg("u", 0, 0, 100, 0),
        seg("v", 0, 5, 100, 5),
        seg("u2", 100, 0, 200, 0),
        seg("v2", 100, 5, 200, 5),
    ]
    out = match_points(network, pts((50, 2.5), (150, 2.5)), 30)
    assert out.status == STATUS_REVIEW
    assert out.reason == "AMBIGUOUS"
    # u/u2, u/v2, v/u2, v/v2 all cost 0.
    assert out.path_count == 2  # capped count
