"""CSV parsing and validation for network and GPS uploads."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite

from .matching import Segment, TrackPoint


class UploadError(ValueError):
    """Raised when an uploaded CSV cannot be accepted."""


COMMON_HEADERS = {
    "路段号": "segment_id",
    "segment_id": "segment_id",
    "segment": "segment_id",
    "起点x": "ax",
    "起点X": "ax",
    "起点经度": "ax",
    "start_x": "ax",
    "ax": "ax",
    "起点y": "ay",
    "起点Y": "ay",
    "起点纬度": "ay",
    "start_y": "ay",
    "ay": "ay",
    "终点x": "bx",
    "终点X": "bx",
    "终点经度": "bx",
    "end_x": "bx",
    "bx": "bx",
    "终点y": "by",
    "终点Y": "by",
    "终点纬度": "by",
    "end_y": "by",
    "by": "by",
    "时刻": "ts",
    "时间": "ts",
    "timestamp": "ts",
    "time": "ts",
    "ts": "ts",
    "x": "x",
    "y": "y",
}

NETWORK_FIELDS = ("segment_id", "ax", "ay", "bx", "by")
POINT_FIELDS = ("ts", "x", "y")


@dataclass(frozen=True)
class ParsedNetwork:
    segments: list[Segment]


@dataclass(frozen=True)
class ParsedPoints:
    points: list[TrackPoint]


def decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadError("CSV 必须是 UTF-8 或 GB18030 编码的文本")


def _read_rows(text: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        raw = list(reader)
    except csv.Error as exc:
        raise UploadError(f"CSV 解析失败：{exc}") from exc
    rows = [
        (line_no, [cell.strip() for cell in row])
        for line_no, row in enumerate(raw, start=1)
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        raise UploadError("CSV 为空")
    return rows[0][1], rows[1:]


def _map_header(header: list[str], required: tuple[str, ...]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, name in enumerate(header):
        key = COMMON_HEADERS.get(name)
        if key and key not in mapping:
            mapping[key] = idx
    missing = [f for f in required if f not in mapping]
    if missing:
        readable = {
            "segment_id": "路段号",
            "ax": "起点x",
            "ay": "起点y",
            "bx": "终点x",
            "by": "终点y",
            "ts": "时刻",
            "x": "x",
            "y": "y",
        }
        raise UploadError(
            "缺少列：" + "、".join(readable[f] for f in missing)
        )
    return mapping


def _parse_float(
    value: str, *, line_no: int, column: str, allow_negative: bool = True
) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise UploadError(
            f"第 {line_no} 行 {column} 不是数字：{value!r}"
        ) from exc
    if not isfinite(number):
        raise UploadError(f"第 {line_no} 行 {column} 不是有限数字")
    if not allow_negative and number < 0:
        raise UploadError(f"第 {line_no} 行 {column} 不能为负")
    return number


def _cell(row: list[str], idx: int) -> str:
    return row[idx] if idx < len(row) else ""


def parse_network(text: str) -> ParsedNetwork:
    header, rows = _read_rows(text)
    mapping = _map_header(header, NETWORK_FIELDS)
    if not rows:
        raise UploadError("路网 CSV 没有数据行")

    segments: list[Segment] = []
    seen: set[str] = set()
    for line_no, row in rows:
        sid = _cell(row, mapping["segment_id"])
        if not sid:
            raise UploadError(f"第 {line_no} 行路段号为空")
        if sid in seen:
            raise UploadError(f"第 {line_no} 行路段号重复：{sid}")
        seen.add(sid)
        values = {
            name: _parse_float(
                _cell(row, mapping[name]),
                line_no=line_no,
                column=name,
            )
            for name in ("ax", "ay", "bx", "by")
        }
        if values["ax"] == values["bx"] and values["ay"] == values["by"]:
            raise UploadError(f"第 {line_no} 行路段 {sid} 起终点重合")
        segments.append(Segment(sid, **values))
    return ParsedNetwork(segments=segments)


def _normalise_timestamp(value: str, line_no: int) -> tuple[str, datetime]:
    """Return a canonical UTC string; offsets in input are converted to UTC."""
    text = value.strip()
    if not text:
        raise UploadError(f"第 {line_no} 行时刻为空")
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise UploadError(f"第 {line_no} 行时刻格式无法识别：{text!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".")
    return canonical + "Z", parsed


def parse_points(text: str) -> ParsedPoints:
    header, rows = _read_rows(text)
    mapping = _map_header(header, POINT_FIELDS)
    if not rows:
        raise UploadError("定位点 CSV 没有数据行")

    points: list[TrackPoint] = []
    previous: datetime | None = None
    for seq, (line_no, row) in enumerate(rows, start=1):
        ts_raw = _cell(row, mapping["ts"])
        ts, parsed = _normalise_timestamp(ts_raw, line_no)
        if previous is not None and parsed < previous:
            raise UploadError(
                f"第 {line_no} 行时刻早于上一行，定位点必须按 UTC 升序"
            )
        previous = parsed
        x = _parse_float(
            _cell(row, mapping["x"]), line_no=line_no, column="x"
        )
        y = _parse_float(
            _cell(row, mapping["y"]), line_no=line_no, column="y"
        )
        points.append(TrackPoint(seq=seq, ts=ts, x=x, y=y))
    return ParsedPoints(points=points)
