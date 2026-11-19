"""FastAPI web application and background worker."""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from math import isfinite
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import quote

from . import db, service
from .parsing import UploadError, decode_csv, parse_network, parse_points

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get(
    "TRACK_MATCHER_DB", str(BASE_DIR.parent / "data" / "track_matcher.sqlite3")
)
WORKER_ENABLED = os.environ.get("TRACK_MATCHER_WORKER", "1") != "0"
MAX_UPLOAD_BYTES = int(os.environ.get("TRACK_MATCHER_MAX_UPLOAD", 20 * 1024 * 1024))

logging.basicConfig(
    level=os.environ.get("TRACK_MATCHER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("track_matcher")

REASON_TEXT = {
    "NO_CANDIDATE": "存在匹配半径内没有任何路段的定位点",
    "NO_CONNECTED_PATH": "候选路段之间不存在满足行驶方向的可连通路径",
    "AMBIGUOUS": "最小距离和对应多条轨迹，无法给出唯一轨迹",
}

STATUS_TEXT = {"valid": "已发布唯一轨迹", "review": "待核"}

JOB_TEXT = {
    "pending": "待重算",
    "running": "计算中",
    "done": "已完成",
    "stale": "已作废（来源或参数已更换）",
}

SOURCE_REASON_TEXT = {
    "NETWORK_REPLACED": "路网换版",
    "POINTS_REPLACED": "定位点换版",
    "RADIUS_CHANGED": "匹配半径变更",
    "SUPERSEDED": "被更新的结果取代",
}


class Worker(threading.Thread):
    """Single background thread that claims and executes jobs."""

    def __init__(self, db_path: str):
        super().__init__(name="track-matcher-worker", daemon=True)
        self.db_path = db_path
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()

    def wake(self) -> None:
        self.wake_event.set()

    def run(self) -> None:
        conn = db.connect(self.db_path)
        try:
            while not self.stop_event.is_set():
                job = None
                try:
                    job = service.claim_job(conn)
                    if job is None:
                        self.wake_event.wait(0.5)
                        self.wake_event.clear()
                        continue
                    log.info(
                        "computing job %s (network r%s, points r%s, radius %s)",
                        job["id"],
                        job["network_revision_id"],
                        job["points_revision_id"],
                        job["radius"],
                    )
                    service.execute_job(conn, job)
                    log.info("job %s finished", job["id"])
                except Exception:  # noqa: BLE001 - worker must survive
                    log.exception("job failed")
                    if job is not None:
                        try:
                            service.fail_job(
                                conn,
                                job["id"],
                                job["claim_token"] or "",
                                "计算失败，稍后自动重试",
                                job["attempts"] or 0,
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("could not record job failure")
                    self.stop_event.wait(1.0)
        finally:
            conn.close()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        self.join(timeout=5)


worker: Worker | None = None
_worker_lock = threading.Lock()


def get_worker() -> Worker | None:
    return worker


def notify_worker() -> None:
    w = get_worker()
    if w is not None:
        w.wake()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    conn = db.connect(DB_PATH)
    try:
        db.init_db(conn)
        reset = service.reset_running_jobs(conn)
        if reset:
            log.info("reset %s interrupted job(s) to pending", reset)
    finally:
        conn.close()
    if WORKER_ENABLED:
        with _worker_lock:
            worker = Worker(DB_PATH)
            worker.start()
    try:
        yield
    finally:
        with _worker_lock:
            if worker is not None:
                worker.stop()
                worker = None


app = FastAPI(title="公交行驶轨迹整理", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _conn():
    return db.connect(DB_PATH)


def _redirect(path: str, notice: str | None = None, error: str | None = None):
    params = []
    if notice:
        params.append("notice=" + quote(notice))
    if error:
        params.append("error=" + quote(error))
    url = path
    if params:
        url += "?" + "&".join(params)
    return RedirectResponse(url=url, status_code=303)


def _result_context(conn, result_row) -> dict:
    assignments = json.loads(result_row["assignments_json"])
    candidates = json.loads(result_row["candidates_json"])
    diagnostics = json.loads(result_row["diagnostics_json"])
    points = conn.execute(
        """
        SELECT seq, ts, x, y FROM track_points
        WHERE revision_id = ? ORDER BY seq
        """,
        (result_row["points_revision_id"],),
    ).fetchall()
    totals = diagnostics.get("candidate_totals") or []
    rows = []
    assignment_by_seq = {a["point_seq"]: a for a in assignments}
    for index, point in enumerate(points):
        cands = candidates[index] if index < len(candidates) else []
        shown = "、".join(
            f"{c['segment_id']}({c['distance']:.3f}m)" for c in cands
        )
        if not shown:
            shown = "半径内无路段"
        elif index < len(totals) and totals[index] > len(cands):
            shown += f" …等共 {totals[index]} 条"
        rows.append(
            {
                "point": point,
                "assignment": assignment_by_seq.get(point["seq"]),
                "candidates": cands,
                "candidate_text": shown,
                "no_candidate": not cands,
            }
        )
    sequence = []
    segment_runs = []
    for item in assignments:
        if not sequence or sequence[-1] != item["segment_id"]:
            sequence.append(item["segment_id"])
            segment_runs.append(item)
    return {
        "segment_runs": segment_runs,
        "result": result_row,
        "assignments": assignments,
        "rows": rows,
        "sequence": sequence,
        "diagnostics": diagnostics,
        "reason_text": REASON_TEXT.get(
            result_row["reason"] or "", result_row["reason"] or ""
        ),
        "status_text": STATUS_TEXT.get(result_row["status"], result_row["status"]),
        "invalidation_text": SOURCE_REASON_TEXT.get(
            result_row["invalidated_reason"] or "",
            result_row["invalidated_reason"] or "",
        ),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, notice: str | None = None, error: str | None = None):
    conn = _conn()
    try:
        network_revision = db.get_revision(
            conn, "network", db.get_active_revision_id(conn, "network")
        )
        points_revision = db.get_revision(
            conn, "points", db.get_active_revision_id(conn, "points")
        )
        radius = db.get_radius(conn)
        result_row = service.current_result(conn)
        result_ctx = _result_context(conn, result_row) if result_row else None
        job = service.latest_job(conn)
        jobs = conn.execute(
            """
            SELECT j.*, nr.revision_no AS network_rev_no,
                   pr.revision_no AS points_rev_no
            FROM jobs j
            JOIN network_revisions nr ON nr.id = j.network_revision_id
            JOIN points_revisions pr ON pr.id = j.points_revision_id
            ORDER BY j.id DESC LIMIT 10
            """
        ).fetchall()
        network_revisions = db.list_revisions(conn, "network", 10)
        points_revisions = db.list_revisions(conn, "points", 10)
        stale_results = conn.execute(
            """
            SELECT r.*, nr.revision_no AS network_rev_no,
                   pr.revision_no AS points_rev_no
            FROM results r
            JOIN network_revisions nr ON nr.id = r.network_revision_id
            JOIN points_revisions pr ON pr.id = r.points_revision_id
            WHERE r.invalidated_at IS NOT NULL
            ORDER BY r.id DESC LIMIT 10
            """
        ).fetchall()
    finally:
        conn.close()

    pending = bool(
        job and job["state"] in ("pending", "running")
        and network_revision and points_revision
        and job["network_revision_id"] == network_revision["id"]
        and job["points_revision_id"] == points_revision["id"]
        and abs(job["radius"] - radius) <= 1e-12
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "network_revision": network_revision,
            "points_revision": points_revision,
            "radius": radius,
            "result_ctx": result_ctx,
            "job": job,
            "jobs": jobs,
            "job_text": JOB_TEXT,
            "network_revisions": network_revisions,
            "points_revisions": points_revisions,
            "stale_results": stale_results,
            "source_reason_text": SOURCE_REASON_TEXT,
            "notice": notice,
            "error": error,
            "pending": pending,
            "worker_enabled": WORKER_ENABLED,
        },
    )


@app.post("/network")
async def upload_network(
    file: UploadFile = File(...),
    note: str = Form(""),
):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return _redirect("/", error="路网 CSV 超过大小上限")
    if not raw.strip():
        return _redirect("/", error="路网 CSV 为空")
    try:
        parsed = parse_network(decode_csv(raw))
    except UploadError as exc:
        return _redirect("/", error=f"路网 CSV 未受理：{exc}")
    conn = _conn()
    try:
        revision_id = service.accept_network_revision(
            conn, parsed, (file.filename or "network.csv")[:200], note.strip()[:200], raw
        )
        revision = conn.execute(
            "SELECT revision_no FROM network_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
    finally:
        conn.close()
    notify_worker()
    return _redirect(
        "/",
        notice=(
            f"路网修订 {revision['revision_no']} 已受理，共 "
            f"{len(parsed.segments)} 条路段；依赖旧路网的轨迹已失效并待重算"
        ),
    )


@app.post("/points")
async def upload_points(
    file: UploadFile = File(...),
    note: str = Form(""),
):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return _redirect("/", error="定位点 CSV 超过大小上限")
    if not raw.strip():
        return _redirect("/", error="定位点 CSV 为空")
    try:
        parsed = parse_points(decode_csv(raw))
    except UploadError as exc:
        return _redirect("/", error=f"定位点 CSV 未受理：{exc}")
    conn = _conn()
    try:
        revision_id = service.accept_points_revision(
            conn, parsed, (file.filename or "points.csv")[:200], note.strip()[:200], raw
        )
        revision = conn.execute(
            "SELECT revision_no FROM points_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
    finally:
        conn.close()
    notify_worker()
    return _redirect(
        "/",
        notice=(
            f"定位点修订 {revision['revision_no']} 已受理，共 "
            f"{len(parsed.points)} 个点；依赖旧定位点的轨迹已失效并待重算"
        ),
    )


@app.post("/radius")
def update_radius(radius: str = Form(...)):
    try:
        value = float(radius)
    except ValueError:
        return _redirect("/", error="匹配半径必须是数字")
    if not isfinite(value) or value <= 0 or value > 100000:
        return _redirect("/", error="匹配半径必须大于 0 且不超过 100000 米")
    conn = _conn()
    try:
        service.set_radius(conn, value)
    finally:
        conn.close()
    notify_worker()
    return _redirect("/", notice=f"匹配半径已设为 {value:g} 米，已安排重算")


@app.post("/recompute")
def recompute():
    conn = _conn()
    try:
        job_id = service.enqueue_recompute(conn)
    finally:
        conn.close()
    if job_id is None:
        return _redirect("/", error="请先上传路网和定位点各一个修订")
    notify_worker()
    return _redirect("/", notice="已安排重算")


@app.get("/results/{result_id}", response_class=HTMLResponse)
def result_detail(request: Request, result_id: int):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM results WHERE id = ?", (result_id,)
        ).fetchone()
        if row is None:
            return _redirect("/", error="结果不存在")
        ctx = _result_context(conn, row)
        network_revision = db.get_revision(
            conn, "network", row["network_revision_id"]
        )
        points_revision = db.get_revision(
            conn, "points", row["points_revision_id"]
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "result.html",
        {
            **ctx,
            "network_revision": network_revision,
            "points_revision": points_revision,
            "source_reason_text": SOURCE_REASON_TEXT,
        },
    )


@app.get("/health")
def health():
    conn = _conn()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}
