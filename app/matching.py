"""Map ordered vehicle positions to directed road segments.

Rules implemented here:
* a point may only be assigned to a segment whose Euclidean distance is
  within the configured radius;
* for two adjacent points the segment may stay the same or move to a segment
  whose start endpoint equals the previous segment's end endpoint (travel
  direction is respected);
* the assignment with the smallest total point-to-segment distance over the
  whole trip is returned only when it is unique.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import floor, hypot
from typing import Iterable, Sequence

# Endpoint coordinates are compared after quantisation at this precision (m).
ENDPOINT_DECIMALS = 6
# A distance this close to the radius still counts as inside the radius.
RADIUS_EPS = 1e-9
# Relative/absolute tolerance used when comparing two path distances.
DIST_ABS_EPS = 1e-9
DIST_REL_EPS = 1e-12
# Grid-index budget; beyond this the matcher scans segments linearly.
MAX_INDEX_ENTRIES = 4_000_000
# Number of nearest candidates persisted per point for the web display.
CANDIDATE_DISPLAY_LIMIT = 20

STATUS_VALID = "valid"
STATUS_REVIEW = "review"

REASON_NO_CANDIDATE = "NO_CANDIDATE"
REASON_NO_CONNECTED_PATH = "NO_CONNECTED_PATH"
REASON_AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Segment:
    segment_id: str
    ax: float
    ay: float
    bx: float
    by: float

    @property
    def length(self) -> float:
        return hypot(self.bx - self.ax, self.by - self.ay)


@dataclass(frozen=True)
class TrackPoint:
    seq: int
    ts: str
    x: float
    y: float


@dataclass
class MatchOutcome:
    status: str
    reason: str | None = None
    assignments: list[dict] = field(default_factory=list)
    total_distance: float | None = None
    path_count: int = 0
    diagnostics: dict = field(default_factory=dict)
    # Snapshot of each point's nearest in-radius candidates, sorted by distance
    # (capped at CANDIDATE_DISPLAY_LIMIT for storage).
    candidates: list[list[dict]] = field(default_factory=list)
    # Total number of in-radius candidates per point, before the display cap.
    candidate_totals: list[int] = field(default_factory=list)


def point_segment_distance(px: float, py: float, seg: Segment) -> float:
    """Euclidean distance from a point to a finite line segment."""
    vx = seg.bx - seg.ax
    vy = seg.by - seg.ay
    length2 = vx * vx + vy * vy
    if length2 == 0.0:
        return hypot(px - seg.ax, py - seg.ay)
    t = ((px - seg.ax) * vx + (py - seg.ay) * vy) / length2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return hypot(px - (seg.ax + t * vx), py - (seg.ay + t * vy))


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= DIST_ABS_EPS + DIST_REL_EPS * max(abs(a), abs(b))


def _node_key(x: float, y: float) -> tuple[float, float]:
    return (round(x, ENDPOINT_DECIMALS), round(y, ENDPOINT_DECIMALS))


def build_successors(segments: Sequence[Segment]) -> dict[str, set[str]]:
    """For each segment, list segments reachable by moving forwards.

    A segment may be followed by itself (staying on it) or by any segment
    whose start endpoint is exactly the current segment's end endpoint.
    """
    starts: dict[tuple[float, float], list[str]] = defaultdict(list)
    for seg in segments:
        starts[_node_key(seg.ax, seg.ay)].append(seg.segment_id)
    successors: dict[str, set[str]] = {}
    for seg in segments:
        reachable = set(starts.get(_node_key(seg.bx, seg.by), ()))
        reachable.add(seg.segment_id)
        successors[seg.segment_id] = reachable
    return successors


class CandidateIndex:
    """Uniform-grid index that returns segments within the matching radius.

    Cells are at least as wide as the radius, so a point only needs to inspect
    its 3x3 block of cells. Segments are rasterised along their length at
    half-cell steps instead of filling their whole bounding box: this keeps
    memory linear in segment length even for long diagonal segments.
    """

    def __init__(self, segments: Sequence[Segment], radius: float):
        self._segments = list(segments)
        self._radius = radius
        lengths = sorted(s.length for s in self._segments)
        median_length = lengths[len(lengths) // 2] if lengths else 0.0
        # cell >= radius is required for the 3x3 query block to be complete.
        self._cell = max(radius, median_length / 4.0, 1e-3)
        self._grid: dict[tuple[int, int], list[int]] | None = defaultdict(list)
        self._entries = 0
        for idx, seg in enumerate(self._segments):
            self._insert(idx, seg)
            if self._grid is not None and self._entries > MAX_INDEX_ENTRIES:
                # Degenerate geometry (very long segments, tiny radius):
                # fall back to a linear scan rather than use unbounded memory.
                self._grid = None
        if self._grid is None:
            self._entries = 0

    def _insert(self, idx: int, seg: Segment) -> None:
        if self._grid is None:
            return
        cell = self._cell
        length = seg.length
        steps = int(length / (cell / 2.0)) + 1
        inserted: set[tuple[int, int]] = set()
        for step in range(steps + 1):
            t = 1.0 if steps == 0 else step / steps
            sx = seg.ax + (seg.bx - seg.ax) * t
            sy = seg.ay + (seg.by - seg.ay) * t
            cx = floor(sx / cell)
            cy = floor(sy / cell)
            # Sampling at <= cell/2 means every cell the segment crosses is
            # within the 3x3 block of some sample's cell.
            for ix in (cx - 1, cx, cx + 1):
                for iy in (cy - 1, cy, cy + 1):
                    key = (ix, iy)
                    if key in inserted:
                        continue
                    inserted.add(key)
                    self._grid[key].append(idx)
                    self._entries += 1

    def candidates(self, point: TrackPoint) -> list[tuple[Segment, float]]:
        cell = self._cell
        cx = floor(point.x / cell)
        cy = floor(point.y / cell)
        if self._grid is None:
            indices: Iterable[int] = range(len(self._segments))
        else:
            seen: set[int] = set()
            gathered: list[int] = []
            # A segment is sampled at <= cell/2 steps and each sample cell is
            # indexed with a 1-cell ring. A point within radius (<= cell) of
            # the segment therefore finds it within 2 cells of its own cell.
            for ix in range(cx - 2, cx + 3):
                for iy in range(cy - 2, cy + 3):
                    for idx in self._grid.get((ix, iy), ()):
                        if idx not in seen:
                            seen.add(idx)
                            gathered.append(idx)
            indices = gathered
        result: list[tuple[Segment, float]] = []
        for idx in indices:
            seg = self._segments[idx]
            distance = point_segment_distance(point.x, point.y, seg)
            if distance <= self._radius + RADIUS_EPS:
                result.append((seg, distance))
        result.sort(key=lambda item: (item[1], item[0].segment_id))
        return result


def candidate_lists(
    segments: Sequence[Segment], points: Sequence[TrackPoint], radius: float
) -> list[list[tuple[Segment, float]]]:
    index = CandidateIndex(segments, radius)
    return [index.candidates(point) for point in points]


def match_points(
    segments: Sequence[Segment], points: Sequence[TrackPoint], radius: float
) -> MatchOutcome:
    candidates = candidate_lists(segments, points, radius)
    candidate_snapshot = [
        [
            {"segment_id": seg.segment_id, "distance": distance}
            for seg, distance in cands[:CANDIDATE_DISPLAY_LIMIT]
        ]
        for cands in candidates
    ]
    candidate_totals = [len(cands) for cands in candidates]

    if not points:
        return MatchOutcome(
            status=STATUS_REVIEW,
            reason=REASON_NO_CANDIDATE,
            diagnostics={"reason": REASON_NO_CANDIDATE, "point_index": None},
            candidates=[],
            candidate_totals=[],
        )

    for idx, cands in enumerate(candidates):
        if not cands:
            return MatchOutcome(
                status=STATUS_REVIEW,
                reason=REASON_NO_CANDIDATE,
                diagnostics={
                    "reason": REASON_NO_CANDIDATE,
                    "point_index": idx,
                    "candidate_display_limit": CANDIDATE_DISPLAY_LIMIT,
                },
                candidates=candidate_snapshot,
                candidate_totals=candidate_totals,
            )

    successors = build_successors(segments)

    # states[segment_id] = (best_cost, number_of_optimal_prefixes_capped_at_2,
    #                       predecessor_segment_id)
    first_cands = candidates[0]
    states: dict[str, tuple[float, int, str | None]] = {
        seg.segment_id: (distance, 1, None) for seg, distance in first_cands
    }
    layers: list[dict[str, tuple[float, int, str | None]]] = [states]
    candidate_maps: list[dict[str, float]] = [
        {seg.segment_id: distance for seg, distance in cands}
        for cands in candidates
    ]

    for idx in range(1, len(points)):
        new_states: dict[str, tuple[float, int, str | None]] = {}
        for seg, distance in candidates[idx]:
            sid = seg.segment_id
            best = float("inf")
            ways = 0
            back: str | None = None
            for prev_id, (prev_cost, prev_ways, _) in states.items():
                if prev_id != sid and sid not in successors.get(prev_id, ()):
                    continue
                cost = prev_cost + distance
                if best == float("inf"):
                    best, ways, back = cost, prev_ways, prev_id
                elif cost < best and not _close(cost, best):
                    best, ways, back = cost, prev_ways, prev_id
                elif _close(cost, best):
                    if cost < best:
                        best = cost
                    ways = min(2, ways + prev_ways)
                    if back is None:
                        back = prev_id
            if ways > 0:
                new_states[sid] = (best, ways, back)
        if not new_states:
            return MatchOutcome(
                status=STATUS_REVIEW,
                reason=REASON_NO_CONNECTED_PATH,
                diagnostics={
                    "reason": REASON_NO_CONNECTED_PATH,
                    "point_index": idx,
                    "candidate_display_limit": CANDIDATE_DISPLAY_LIMIT,
                },
                candidates=candidate_snapshot,
                candidate_totals=candidate_totals,
            )
        states = new_states
        layers.append(states)

    min_cost = min(cost for cost, _, _ in states.values())
    optimal = [
        (sid, cost, ways)
        for sid, (cost, ways, _) in states.items()
        if _close(cost, min_cost)
    ]
    total_ways = min(2, sum(ways for _, _, ways in optimal))

    if total_ways > 1:
        return MatchOutcome(
            status=STATUS_REVIEW,
            reason=REASON_AMBIGUOUS,
            total_distance=min_cost,
            path_count=total_ways,
            diagnostics={
                "reason": REASON_AMBIGUOUS,
                "path_count": total_ways,
                "min_total_distance": min_cost,
                "candidate_display_limit": CANDIDATE_DISPLAY_LIMIT,
            },
            candidates=candidate_snapshot,
            candidate_totals=candidate_totals,
        )

    final_id = optimal[0][0]
    segment_sequence: list[str] = []
    sid: str | None = final_id
    for idx in range(len(points) - 1, -1, -1):
        if sid is None:  # pragma: no cover - defensive, should not happen
            raise RuntimeError("broken traceback")
        segment_sequence.append(sid)
        sid = layers[idx][sid][2]
    segment_sequence.reverse()

    assignments = []
    for idx, point in enumerate(points):
        sid = segment_sequence[idx]
        assignments.append(
            {
                "point_seq": point.seq,
                "segment_id": sid,
                "distance": candidate_maps[idx][sid],
                "candidate_count": len(candidates[idx]),
            }
        )

    return MatchOutcome(
        status=STATUS_VALID,
        assignments=assignments,
        total_distance=min_cost,
        path_count=1,
        diagnostics={
            "reason": None,
            "candidate_display_limit": CANDIDATE_DISPLAY_LIMIT,
        },
        candidates=candidate_snapshot,
        candidate_totals=candidate_totals,
    )


def compress_segment_sequence(segment_sequence: Iterable[str]) -> list[str]:
    compressed: list[str] = []
    for sid in segment_sequence:
        if not compressed or compressed[-1] != sid:
            compressed.append(sid)
    return compressed
