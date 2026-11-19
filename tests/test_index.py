import random

from app.matching import CandidateIndex, Segment, TrackPoint, point_segment_distance


def brute_candidates(segments, point, radius):
    return sorted(
        s.segment_id
        for s in segments
        if point_segment_distance(point.x, point.y, s) <= radius + 1e-9
    )


def test_grid_index_matches_brute_force_on_random_geometry():
    rng = random.Random(20261118)
    for trial in range(25):
        count = rng.randint(1, 40)
        segments = []
        for i in range(count):
            ax = rng.uniform(-300, 300)
            ay = rng.uniform(-300, 300)
            length = rng.choice([0.5, 5, 30, 120, 400])
            angle = rng.uniform(0, 6.28318)
            from math import cos, sin

            bx = ax + length * cos(angle)
            by = ay + length * sin(angle)
            segments.append(Segment(f"S{i}", ax, ay, bx, by))
        radius = rng.choice([0.5, 2, 10, 50])
        index = CandidateIndex(segments, radius)
        for _ in range(60):
            p = TrackPoint(1, "t", rng.uniform(-350, 350), rng.uniform(-350, 350))
            got = sorted(s.segment_id for s, _ in index.candidates(p))
            assert got == brute_candidates(segments, p, radius)
