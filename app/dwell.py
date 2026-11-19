"""站点驻留核对的纯计算逻辑。

核对对象是一条已发布的快照轨迹（路网匹配结果）中的逐点归属。规则：

* 只取归属路段号等于站点路段号、且到站心平面距离不超过判定半径的定位点；
* 相邻点时间间隔全部不超过最大采样间隔的连续段，才视为连续观测；
* 连续段首末时差达到最短驻留时长 → ``confirmed``（已驻留），
  并给出观察到的起止时刻与证据点；
* 存在符合空间条件、但因间隔超限被隔开的点 → 断采样，``insufficient``
  （断采样不能拼成驻留）；
* 轨迹本身待核（无唯一轨迹）→ ``unavailable``，不得给出已驻留结论。

该模块不做任何 I/O，时间戳解析与 ``parsing._normalise_timestamp`` 的口径
保持一致（UTC ISO 字符串）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import hypot

STATUS_CONFIRMED = "confirmed"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNAVAILABLE = "unavailable"

REASON_TRAJECTORY_UNDER_REVIEW = "TRAJECTORY_UNDER_REVIEW"
REASON_NO_QUALIFYING_POINTS = "NO_QUALIFYING_POINTS"
REASON_GAP_EXCEEDS_LIMIT = "GAP_EXCEEDS_LIMIT"
REASON_SPAN_TOO_SHORT = "SPAN_TOO_SHORT"

# 时间差比较容差（秒）：吸收 ISO 字符串往返的浮点噪声。
TIME_EPS = 1e-6
# 判定半径比较容差（米）：与 matching.RADIUS_EPS 同量级。
RADIUS_EPS = 1e-9

REASON_TEXT = {
    REASON_TRAJECTORY_UNDER_REVIEW: "现行轨迹待核，没有唯一轨迹可用于核对",
    REASON_NO_QUALIFYING_POINTS: "轨迹中没有任何定位点同时归属该路段且落入站点半径",
    REASON_GAP_EXCEEDS_LIMIT: "符合路段与半径条件的点之间存在超过最大采样间隔的断档，断采样不能拼成驻留",
    REASON_SPAN_TOO_SHORT: "最长连续观测段的首末时差未达到最短驻留时长",
}


def parse_utc(ts: str) -> datetime:
    """Parse the canonical UTC strings stored for track points."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass(frozen=True)
class DwellParams:
    segment_id: str
    x: float
    y: float
    radius: float
    min_dwell_seconds: float
    max_gap_seconds: float


@dataclass
class DwellEvaluation:
    status: str
    reason: str | None = None
    observed_start: str | None = None
    observed_end: str | None = None
    span_seconds: float | None = None
    evidence: list[dict] = field(default_factory=list)
    qualifying_count: int = 0
    gap_count: int = 0
    diagnostics: dict = field(default_factory=dict)


def evaluate_dwell(
    trajectory_status: str,
    points: list[dict],
    params: DwellParams,
) -> DwellEvaluation:
    """Evaluate one dwell check against a snapshot trajectory.

    ``points`` are dicts with keys ``seq``, ``ts``, ``x``, ``y`` and
    ``segment_id`` (the point's assigned segment, or ``None``), ordered by
    ``seq``. ``trajectory_status`` is the bound matching result's status.
    """
    if trajectory_status != "valid":
        return DwellEvaluation(
            status=STATUS_UNAVAILABLE,
            reason=REASON_TRAJECTORY_UNDER_REVIEW,
            diagnostics={"trajectory_status": trajectory_status},
        )

    qualifying: list[dict] = []
    for point in points:
        if point.get("segment_id") != params.segment_id:
            continue
        distance = hypot(point["x"] - params.x, point["y"] - params.y)
        if distance <= params.radius + RADIUS_EPS:
            qualifying.append(
                {
                    "seq": point["seq"],
                    "ts": point["ts"],
                    "x": point["x"],
                    "y": point["y"],
                    "distance": distance,
                    "moment": parse_utc(point["ts"]),
                }
            )

    if not qualifying:
        return DwellEvaluation(
            status=STATUS_INSUFFICIENT,
            reason=REASON_NO_QUALIFYING_POINTS,
            qualifying_count=0,
            diagnostics={"segment_id": params.segment_id},
        )

    # Split into runs: a run breaks wherever the sampling interval between
    # two consecutive qualifying points exceeds the limit.
    runs: list[list[dict]] = [[qualifying[0]]]
    gap_count = 0
    for prev, cur in zip(qualifying, qualifying[1:]):
        gap = (cur["moment"] - prev["moment"]).total_seconds()
        if gap <= params.max_gap_seconds + TIME_EPS:
            runs[-1].append(cur)
        else:
            gap_count += 1
            runs.append([cur])

    def run_span(run: list[dict]) -> float:
        return (run[-1]["moment"] - run[0]["moment"]).total_seconds()

    best = max(runs, key=lambda run: (run_span(run), len(run)))
    best_span = run_span(best)

    if best_span >= params.min_dwell_seconds - TIME_EPS:
        evidence = [
            {
                "seq": p["seq"],
                "ts": p["ts"],
                "x": p["x"],
                "y": p["y"],
                "distance": p["distance"],
            }
            for p in best
        ]
        return DwellEvaluation(
            status=STATUS_CONFIRMED,
            observed_start=best[0]["ts"],
            observed_end=best[-1]["ts"],
            span_seconds=best_span,
            evidence=evidence,
            qualifying_count=len(qualifying),
            gap_count=gap_count,
            diagnostics={
                "run_count": len(runs),
                "evidence_count": len(best),
            },
        )

    reason = REASON_GAP_EXCEEDS_LIMIT if gap_count else REASON_SPAN_TOO_SHORT
    return DwellEvaluation(
        status=STATUS_INSUFFICIENT,
        reason=reason,
        qualifying_count=len(qualifying),
        gap_count=gap_count,
        diagnostics={
            "run_count": len(runs),
            "longest_run_span_seconds": best_span,
            "longest_run_points": len(best),
            "longest_run_start": best[0]["ts"],
            "longest_run_end": best[-1]["ts"],
        },
    )
